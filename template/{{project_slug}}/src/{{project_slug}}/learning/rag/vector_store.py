"""
VectorStore -- lightweight vector-search adapter with persistent fallback.

Provides project-isolated vector storage for semantic search over preferences,
feedback, and any other text the learning system needs to retrieve.

By default: uses ChromaDB persistent storage when ChromaDB is installed, and
falls back to a deterministic in-memory cosine similarity store otherwise.
Set `ROUNDTABLE_USE_CHROMA=0` (or pass `use_chroma=False`) for local tests that
need a guaranteed in-memory store.

Production recommendation: if the project uses Postgres, add pgvector and
store embeddings alongside application data so retention, backups, tenant
isolation, and access control stay in one datastore.

Security:
  - Documents are sanitized before indexing (size-limited)
  - Project isolation prevents cross-project data leakage

Keep this file under 250 lines.
"""

import logging
import math
import os
from dataclasses import dataclass, field
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
    Vector storage with ChromaDB auto-detection and in-memory fallback.

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
        if use_chroma is None:
            chroma_setting = (
                os.environ.get("ROUNDTABLE_USE_CHROMA", "auto").strip().lower()
            )
            self._use_chroma = chroma_setting not in {
                "0",
                "false",
                "no",
                "off",
                "memory",
                "in-memory",
            }
        else:
            self._use_chroma = use_chroma
        self._collection: Any = None
        self._fallback_store: list[dict] | None = None

        self._init_store()

    def _init_store(self) -> None:
        """Initialize the configured store or fall back to in-memory."""
        if not self._use_chroma:
            self._fallback_store = []
            logger.info(
                "[VectorStore] ChromaDB disabled -- using in-memory vector store. "
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
        except ImportError:
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
            return self._search_fallback(query, limit, query_embedding)
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
        self, query: str, limit: int, query_embedding: list[float] | None
    ) -> SearchResults:
        """Simple keyword + cosine similarity fallback search."""
        if not self._fallback_store:
            return SearchResults(query=query)

        scored = []
        query_lower = query.lower()
        query_words = set(query_lower.split())

        for doc in self._fallback_store:
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
