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

Keep this file under 445 lines. (Raised from 250, then 400: the Chroma
persistence-preservation logic and the in-memory fallback both live here,
and the fallback now carries two ranking paths -- BM25/RRF plus the
verbatim legacy scorer kept for the LEXICAL_RANKING_ENABLED kill switch.
If a third backend is added, split the adapters apart.)
"""

import logging
import math
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...security.prompt_guard import sanitize_for_prompt

logger = logging.getLogger(__name__)

try:
    from .lexical import (
        bm25_scores,
        lexical_ranking_enabled,
        rrf_fuse,
        tokenize,
    )
    _LEXICAL_AVAILABLE = True
except Exception:  # pragma: no cover -- defensive: module ships in-package
    _LEXICAL_AVAILABLE = False
    logger.warning(
        "[VectorStore] lexical ranking module unavailable; "
        "in-memory search uses the legacy keyword scorer"
    )

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

        env_value = os.environ.get("USE_CHROMA")
        if env_value is None:
            legacy_value = os.environ.get("ROUNDTABLE_USE_CHROMA")
            if legacy_value is not None:
                warnings.warn(
                    "ROUNDTABLE_USE_CHROMA is deprecated and will be ignored in a "
                    "future release; set USE_CHROMA instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                env_value = legacy_value
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
                    "USE_CHROMA=0 to use an ephemeral in-memory store."
                ) from e
            self._fallback_store = []
            logger.info(
                "[VectorStore] ChromaDB not installed -- using in-memory fallback. "
                "For production with Postgres, add a pgvector-backed adapter."
            )

    @property
    def supports_keyword_search(self) -> bool:
        """True when search() can rank lexically (in-memory store: BM25,
        or the legacy keyword scorer with the kill switch off).

        Chroma has no lexical path -- every stored document and every
        query needs an embedding (omitting one makes Chroma compute its
        own default-model embedding, which breaks dimension consistency
        with previously stored vectors). Retrievers use this to decide
        whether skipping non-semantic hash embeddings is safe: only the
        in-memory store can rank without an embedding.
        """
        return self._collection is None

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
        """Search for documents similar to the query.

        CALLER CONTRACT: pass `query_embedding` only if it is semantic --
        the store fuses whatever it is given. Non-semantic hash-fallback
        vectors must be passed as None (the retrievers do; see
        `EmbeddingService.is_semantic`) or their cosine noise joins the
        hybrid ranking.
        """
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
        """BM25 lexical ranking with optional RRF hybrid fusion.

        The semantic arm runs only when the CALLER passes `query_embedding`
        (caller contract: retrievers pass None for non-semantic hash
        embeddings, so hash-cosine never ranks). Flag-off falls back to the
        legacy binary-presence scorer, byte-identically. Membership: docs
        need lexical > 0 or cosine > 0; `total` counts eligible docs.
        Scores are raw BM25 (lexical-only) or RRF sums (hybrid), a
        different scale from the legacy 0-1 fraction.
        """
        if not self._fallback_store:
            return SearchResults(query=query)
        if not _LEXICAL_AVAILABLE or not lexical_ranking_enabled():
            return self._search_fallback_legacy(
                query, limit, where, query_embedding
            )

        candidates = [
            d for d in self._fallback_store
            if self._matches_where(d.get("metadata", {}), where)
        ]
        if not candidates:
            return SearchResults(query=query)

        # Scoped-IDF invariant: BM25 corpus statistics are computed only
        # over the where-passing candidates (see lexical.py docstring).
        lex = bm25_scores(
            tokenize(query), [tokenize(d["content"]) for d in candidates]
        )
        by_id = {d["id"]: d for d in candidates}
        lex_ranked = [
            d["id"]
            for score, d in sorted(
                zip(lex, candidates), key=lambda t: (-t[0], t[1]["id"])
            )
            if score > 0
        ]

        if query_embedding is None:
            eligible = lex_ranked
            score_of = {d["id"]: score for score, d in zip(lex, candidates)}
        else:
            cos: dict[str, float] = {}
            for d in candidates:
                if d.get("embedding"):
                    sim = self._cosine_similarity(query_embedding, d["embedding"])
                    if sim > 0:
                        cos[d["id"]] = sim
            sem_ranked = sorted(cos, key=lambda i: (-cos[i], i))
            score_of = rrf_fuse(lex_ranked, sem_ranked)
            eligible = sorted(score_of, key=lambda i: (-score_of[i], i))

        return SearchResults(
            results=[
                SearchResult(
                    id=doc_id,
                    content=by_id[doc_id]["content"],
                    metadata=by_id[doc_id].get("metadata", {}),
                    score=score_of[doc_id],
                )
                for doc_id in eligible[:limit]
            ],
            total=len(eligible),
            query=query,
        )

    def _search_fallback_legacy(
        self,
        query: str,
        limit: int,
        where: dict | None,
        query_embedding: list[float] | None,
    ) -> SearchResults:
        """Legacy binary keyword-presence + cosine scorer (kill-switch
        path). Preserved verbatim: LEXICAL_RANKING_ENABLED=false must be
        byte-identical to the pre-BM25 release."""
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
