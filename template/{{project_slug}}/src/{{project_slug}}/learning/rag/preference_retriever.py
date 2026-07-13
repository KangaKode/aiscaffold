"""
PreferenceRetriever -- Semantic search over learned preferences.

Indexes UserPreference objects into the VectorStore and retrieves
the most relevant preferences for a given query. Used by the
UserProfileManager to build context bundles for LLM prompts.

Document categories are configurable strings -- projects register
their own category names (e.g., "style", "behavior", "output_format").

Keep this file under 200 lines.
"""

import logging
from pathlib import Path

from ..models import UserPreference
from ..schema import DEFAULT_DB_PATH, dict_from_row, get_connection
from .embedding_service import EmbeddingService
from .vector_store import SearchResults, VectorStore

logger = logging.getLogger(__name__)


class PreferenceRetriever:
    """
    Indexes and retrieves preferences using semantic search.

    Usage:
        retriever = PreferenceRetriever(project_id="my_project")

        # Index all preferences from the database
        retriever.index_from_db()

        # Search for relevant preferences
        results = retriever.search("How verbose should I be?", limit=5)
        for r in results.results:
            print(f"{r.metadata['key']}: {r.content} (score={r.score:.2f})")
    """

    def __init__(
        self,
        project_id: str = "default",
        vector_store: VectorStore | None = None,
        embedding_service: EmbeddingService | None = None,
        db_path: Path = DEFAULT_DB_PATH,
    ):
        self._project_id = project_id
        self._db_path = db_path
        self._store = vector_store or VectorStore(project_id=f"prefs_{project_id}")
        self._embedder = embedding_service or EmbeddingService()

    @property
    def _use_embeddings(self) -> bool:
        """Attach embeddings unless they are non-semantic hash filler AND
        the store can rank lexically (BM25) instead. This is the caller
        contract the hybrid search relies on: passing no embedding keeps
        hash-cosine out of ranking entirely. The Chroma backend always
        gets embeddings: it has no lexical path, and omitting them would
        make Chroma compute default-model vectors that mismatch the
        dimensions of previously stored hash vectors."""
        return self._embedder.is_semantic or not self._store.supports_keyword_search

    def index_preference(self, pref: UserPreference) -> None:
        """Index a single preference into the vector store.

        With a non-semantic (hash fallback) provider AND the in-memory
        store, no embedding is attached so BM25 lexical ranking does the
        work. Chroma has no lexical path and needs dimension-consistent
        vectors, so there the hash embedding is still stored
        (see _use_embeddings)."""
        doc_text = f"{pref.preference_type}: {pref.key} = {pref.value}"

        embedding = (
            self._embedder.embed(doc_text).embedding
            if self._use_embeddings
            else None
        )

        self._store.add(
            doc_id=pref.id,
            content=doc_text,
            metadata={
                "preference_type": pref.preference_type,
                "key": pref.key,
                "value": pref.value,
                "source": pref.source,
                "priority": pref.priority,
                "active": pref.active,
            },
            embedding=embedding,
        )

    def index_from_db(self) -> int:
        """
        Index all active preferences from the database.

        Returns the number of preferences indexed.
        """
        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(
                """SELECT * FROM user_preferences
                   WHERE project_id = ? AND active = 1
                   ORDER BY priority DESC""",
                (self._project_id,),
            ).fetchall()
        finally:
            conn.close()

        count = 0
        for row in rows:
            data = dict_from_row(row)
            pref = UserPreference(
                id=data["id"],
                project_id=data.get("project_id", self._project_id),
                preference_type=data["preference_type"],
                key=data["key"],
                value=data["value"],
                source=data.get("source", "implicit"),
                priority=data.get("priority", 50),
                active=bool(data.get("active", True)),
                metadata=data.get("metadata", {}),
                created_at=data.get("created_at", ""),
                updated_at=data.get("updated_at", ""),
            )
            self.index_preference(pref)
            count += 1

        logger.info(
            f"[PreferenceRetriever] Indexed {count} preferences "
            f"for project {self._project_id}"
        )
        return count

    def search(
        self,
        query: str,
        limit: int = 10,
        preference_type: str | None = None,
        min_priority: int = 0,
    ) -> SearchResults:
        """
        Search for preferences relevant to a query.

        Args:
            query: Natural language query.
            limit: Max results.
            preference_type: Filter by category (optional).
            min_priority: Minimum priority threshold (optional).

        Returns:
            SearchResults with scored matches.
        """
        # Hash-fallback vectors would pollute the in-memory store's lexical
        # ranking via the hybrid cosine arm (measured on the legacy scorer:
        # irrelevant docs can outrank keyword hits), so a query embedding is
        # attached only when it is semantic -- or when the backend is
        # Chroma, which cannot rank without one.
        query_embedding = (
            self._embedder.embed(query).embedding
            if self._use_embeddings
            else None
        )

        where = None
        if preference_type:
            where = {"preference_type": preference_type}

        results = self._store.search(
            query=query,
            limit=limit,
            where=where,
            query_embedding=query_embedding,
        )

        if min_priority > 0:
            results.results = [
                r for r in results.results
                if r.metadata.get("priority", 0) >= min_priority
            ]
            results.total = len(results.results)

        return results

    def clear_index(self) -> None:
        """Clear the vector store index for this project."""
        self._store.clear()
        logger.info(
            f"[PreferenceRetriever] Cleared index for project {self._project_id}"
        )

    @property
    def indexed_count(self) -> int:
        """Number of indexed preferences."""
        return self._store.count
