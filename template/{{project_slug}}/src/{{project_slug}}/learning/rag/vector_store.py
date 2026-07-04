"""
VectorStore -- lightweight vector-search adapter with in-memory fallback.

Provides project-isolated vector storage for semantic search over preferences,
feedback, and any other text the learning system needs to retrieve.

By default: uses a deterministic in-memory cosine similarity store for local
development and tests. Existing Chroma persistence directories are reused
automatically so upgrades do not drop previously indexed search data.

Production recommendation: if the project uses Postgres, add pgvector and
store embeddings alongside application data so retention, backups, tenant
isolation, and access control stay in one datastore. ChromaDB remains a small
optional adapter path for prototypes or single-node deployments.

Security:
  - Documents are sanitized before indexing (size-limited)
  - Project isolation prevents cross-project data leakage

Keep this file under 250 lines.
"""

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...security.prompt_guard import sanitize_for_prompt

logger = logging.getLogger(__name__)

MAX_DOCUMENT_LENGTH = 10_000
MAX_RESULTS = 50


@dataclass
class SearchResult:
    """A single search result from the vector store."""

    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


@dataclass
class SearchResults:
    """Collection of search results."""

    results: list[SearchResult] = field(default_factory=list)
    total: int = 0
    query: str = ""


class VectorStore:
    """
    Vector storage with an in-memory default and optional Chroma adapter.

    Usage:
        store = VectorStore(project_id="my_project")
        store.add("pref_1", "User prefers concise responses", {"type": "style"})
        results = store.search("how verbose should responses be?", limit=5)
    """

    def __init__(
        self,
        project_id: str = "default",
        persist_dir: str = "data/chroma",
        use_chroma: bool | None = None,
    ):
        self._project_id = project_id
        self._persist_dir = persist_dir
        self._use_chroma, self._chroma_required = self._resolve_chroma_mode(
            persist_dir=persist_dir,
            use_chroma=use_chroma,
        )
        self._collection: Any = None
        self._fallback_store: list[dict] | None = None

        self._init_store()

    @staticmethod
    def _resolve_chroma_mode(
        persist_dir: str,
        use_chroma: bool | None,
    ) -> tuple[bool, bool]:
        """Return whether to use Chroma and whether failure should be fatal."""
        if use_chroma is not None:
            return use_chroma, use_chroma

        env_value = os.environ.get("ROUNDTABLE_USE_CHROMA")
        if env_value is not None:
            enabled = env_value.strip().lower() in {"1", "true", "yes", "on"}
            return enabled, enabled

        # Preserve existing persistent indexes during upgrades while keeping
        # brand-new local projects on the deterministic in-memory default.
        has_existing_index = Path(persist_dir).exists()
        return has_existing_index, has_existing_index

    def _init_store(self) -> None:
        """Initialize the configured store or fall back to in-memory."""
        if not self._use_chroma:
            self._fallback_store = []
            logger.info(
                "[VectorStore] Using in-memory vector store. "
                "For production with Postgres, add a pgvector-backed adapter."
            )
            return

        try:
            import chromadb

            client = chromadb.PersistentClient(path=self._persist_dir)
            self._collection = client.get_or_create_collection(
                name=f"learning_{self._project_id}",
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(
                f"[VectorStore] ChromaDB initialized for project {self._project_id}"
            )
        except ImportError as e:
            if self._chroma_required:
                raise RuntimeError(
                    "[VectorStore] Existing or explicitly requested ChromaDB "
                    "storage is unavailable. Install chromadb or set "
                    "ROUNDTABLE_USE_CHROMA=0 to use an ephemeral in-memory store."
                ) from e
            self._fallback_store = []
            logger.info(
                "[VectorStore] ChromaDB not installed -- using in-memory fallback. "
                "For production with Postgres, add a pgvector-backed adapter."
            )

    def add(
        self,
        doc_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
    ) -> None:
        """Add a document to the store."""
        content = sanitize_for_prompt(content, max_length=MAX_DOCUMENT_LENGTH)
        metadata = metadata or {}
        metadata["project_id"] = self._project_id

        if self._collection is not None:
            kwargs: dict[str, Any] = {
                "ids": [doc_id],
                "documents": [content],
                "metadatas": [metadata],
            }
            if embedding:
                kwargs["embeddings"] = [embedding]
            self._collection.upsert(**kwargs)
        elif self._fallback_store is not None:
            existing = [i for i, d in enumerate(self._fallback_store) if d["id"] == doc_id]
            entry = {
                "id": doc_id,
                "content": content,
                "metadata": metadata,
                "embedding": embedding,
            }
            if existing:
                self._fallback_store[existing[0]] = entry
            else:
                self._fallback_store.append(entry)

    def search(
        self,
        query: str,
        limit: int = 10,
        where: dict | None = None,
        query_embedding: list[float] | None = None,
    ) -> SearchResults:
        """Search for documents similar to the query."""
        limit = min(limit, MAX_RESULTS)

        if self._collection is not None:
            return self._search_chroma(query, limit, where, query_embedding)
        elif self._fallback_store is not None:
            return self._search_fallback(query, limit, where, query_embedding)
        return SearchResults(query=query)

    def delete(self, doc_id: str) -> None:
        """Delete a document by ID."""
        if self._collection is not None:
            try:
                self._collection.delete(ids=[doc_id])
            except Exception:
                pass
        elif self._fallback_store is not None:
            self._fallback_store = [
                d for d in self._fallback_store if d["id"] != doc_id
            ]

    def clear(self) -> None:
        """Clear all documents for this project."""
        if self._collection is not None:
            try:
                all_ids = self._collection.get()["ids"]
                if all_ids:
                    self._collection.delete(ids=all_ids)
            except Exception as e:
                logger.warning(f"[VectorStore] Clear failed: {e}")
        elif self._fallback_store is not None:
            self._fallback_store.clear()

    @property
    def count(self) -> int:
        """Number of documents in the store."""
        if self._collection is not None:
            return self._collection.count()
        elif self._fallback_store is not None:
            return len(self._fallback_store)
        return 0

    def _search_chroma(
        self, query: str, limit: int, where: dict | None, query_embedding: list[float] | None
    ) -> SearchResults:
        """Search using ChromaDB."""
        kwargs: dict[str, Any] = {"n_results": limit}
        if query_embedding:
            kwargs["query_embeddings"] = [query_embedding]
        else:
            kwargs["query_texts"] = [query]
        if where:
            kwargs["where"] = where

        try:
            results = self._collection.query(**kwargs)
        except Exception as e:
            logger.error(f"[VectorStore] ChromaDB search failed: {e}")
            return SearchResults(query=query)

        items = []
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]

        for i, doc_id in enumerate(ids):
            score = 1.0 - (dists[i] if i < len(dists) else 0.0)
            items.append(SearchResult(
                id=doc_id,
                content=docs[i] if i < len(docs) else "",
                metadata=metas[i] if i < len(metas) else {},
                score=max(0.0, score),
            ))

        return SearchResults(results=items, total=len(items), query=query)

    def _search_fallback(
        self,
        query: str,
        limit: int,
        where: dict | None,
        query_embedding: list[float] | None,
    ) -> SearchResults:
        """Simple keyword + cosine similarity fallback search."""
        if not self._fallback_store:
            return SearchResults(query=query)

        scored = []
        query_lower = query.lower()
        query_words = set(query_lower.split())

        for doc in self._fallback_store:
            if not self._matches_where(doc.get("metadata", {}), where):
                continue

            score = 0.0

            if query_embedding and doc.get("embedding"):
                score = self._cosine_similarity(query_embedding, doc["embedding"])
            else:
                doc_lower = doc["content"].lower()
                matches = sum(1 for w in query_words if w in doc_lower)
                score = matches / max(len(query_words), 1)

            scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:limit]

        return SearchResults(
            results=[
                SearchResult(
                    id=doc["id"],
                    content=doc["content"],
                    metadata=doc.get("metadata", {}),
                    score=score,
                )
                for score, doc in top
                if score > 0
            ],
            total=len([s for s in scored if s[0] > 0]),
            query=query,
        )

    @staticmethod
    def _matches_where(metadata: dict, where: dict | None) -> bool:
        """Evaluate simple exact-match metadata filters for the fallback store."""
        if not where:
            return True
        return all(metadata.get(key) == value for key, value in where.items())

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
