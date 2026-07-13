"""
TranscriptIndexer -- Semantic search over round table deliberations.

Indexes completed round table results into the VectorStore so past
deliberations can be retrieved by semantic search. Follows the same
pattern as PreferenceRetriever.

Usage:
    indexer = TranscriptIndexer()
    indexer.index_result(round_table_result, task_content="Analyze API design")
    results = indexer.search("authentication best practices", limit=5)

Keep this file under 250 lines. (Raised from 200: the Chroma-aware
embedding gate (_use_embeddings) pushed it just past the budget.)
"""

import hashlib
import logging
from datetime import datetime
from typing import Any

from .embedding_service import EmbeddingService
from .vector_store import SearchResult, SearchResults, VectorStore

logger = logging.getLogger(__name__)


class TranscriptIndexer:
    """
    Indexes and retrieves round table transcripts using semantic search.

    Each round table result is indexed as a single document combining
    the task content, agent analyses, synthesis, and vote outcomes.
    Metadata includes task_id, agent names, consensus status, etc.
    """

    def __init__(
        self,
        vector_store: VectorStore | None = None,
        embedding_service: EmbeddingService | None = None,
    ):
        self._store = vector_store or VectorStore(project_id="round_table_transcripts")
        self._embedder = embedding_service or EmbeddingService()

    @property
    def _use_embeddings(self) -> bool:
        """Attach embeddings unless they are non-semantic hash filler AND
        the store can rank lexically (BM25) instead (Chroma cannot -- see
        PreferenceRetriever._use_embeddings for the full rationale)."""
        return self._embedder.is_semantic or not self._store.supports_keyword_search

    def index_result(
        self,
        result: Any,
        task_content: str = "",
        owner_key: str | None = None,
    ) -> None:
        """Index a round table result for semantic search.

        Combines task content, agent analyses, synthesis recommendation,
        and vote outcomes into a searchable document.

        Args:
            result: A RoundTableResult (imported lazily to avoid circular deps).
            task_content: The original task text submitted by the user.
            owner_key: Optional tenant/user key used to isolate transcript search.
        """
        doc_parts = []

        task_id = getattr(result, "task_id", "unknown")
        doc_parts.append(f"Task ID: {task_id}")

        if task_content:
            doc_parts.append(f"Task: {task_content}")

        for analysis in getattr(result, "analyses", []):
            agent = getattr(analysis, "agent_name", "unknown")
            domain = getattr(analysis, "domain", "")
            doc_parts.append(f"Agent {agent} ({domain}):")
            for obs in getattr(analysis, "observations", []):
                if isinstance(obs, dict):
                    doc_parts.append(
                        f"  - {obs.get('finding', '')} "
                        f"[evidence: {obs.get('evidence', '')}]"
                    )

        synthesis = getattr(result, "synthesis", None)
        if synthesis:
            direction = getattr(synthesis, "recommended_direction", "")
            if direction:
                doc_parts.append(f"Recommendation: {direction}")

        if len(doc_parts) <= 1:
            logger.debug("[TranscriptIndexer] Empty result, skipping indexing")
            return

        doc_text = "\n".join(doc_parts)

        agent_names = ",".join(
            getattr(a, "agent_name", "") for a in getattr(result, "analyses", [])
        )
        consensus = getattr(result, "consensus_reached", False)
        approval = getattr(result, "approval_rate", 0.0)
        duration = getattr(result, "duration_seconds", 0.0)

        # With the in-memory store, meaningless hash-fallback vectors are
        # skipped so BM25 lexical ranking scores results; Chroma always
        # gets an embedding (see _use_embeddings).
        embedding = (
            self._embedder.embed(doc_text).embedding
            if self._use_embeddings
            else None
        )

        metadata = {
            "task_id": task_id,
            "agent_names": agent_names,
            "consensus_reached": str(consensus),
            "approval_rate": str(round(approval, 2)),
            "duration_seconds": str(round(duration, 2)),
            "timestamp": datetime.now().isoformat(),
            "doc_type": "round_table_transcript",
        }
        if owner_key:
            metadata["owner_key"] = owner_key

        self._store.add(
            doc_id=self._doc_id(task_id, owner_key),
            content=doc_text,
            metadata=metadata,
            embedding=embedding,
        )
        logger.debug(f"[TranscriptIndexer] Indexed transcript for task {task_id}")

    def search(
        self,
        query: str,
        limit: int = 10,
        consensus_only: bool = False,
        owner_key: str | None = None,
    ) -> SearchResults:
        """Semantic search over past round table results.

        Args:
            query: Natural language search query.
            limit: Maximum number of results.
            consensus_only: If True, only return results where consensus was reached.
            owner_key: Optional tenant/user key to restrict results.
        """
        # Keyword matching when the provider is the hash fallback and the
        # store supports it; Chroma queries always carry an embedding
        # (see _use_embeddings).
        query_embedding = (
            self._embedder.embed(query).embedding
            if self._use_embeddings
            else None
        )

        where = {"owner_key": owner_key} if owner_key else None
        results = self._store.search(
            query=query,
            limit=limit,
            where=where,
            query_embedding=query_embedding,
        )

        if consensus_only:
            results.results = [
                r for r in results.results
                if r.metadata.get("consensus_reached") == "True"
            ]
            results.total = len(results.results)

        return results

    def get_by_task_id(
        self,
        task_id: str,
        owner_key: str | None = None,
    ) -> SearchResult | None:
        """Direct lookup of a transcript by task ID."""
        results = self._store.search(
            query=task_id,
            limit=50,
            where={"owner_key": owner_key} if owner_key else None,
        )
        doc_id = self._doc_id(task_id, owner_key)
        for r in results.results:
            if r.id == doc_id:
                return r
        return None

    @staticmethod
    def _doc_id(task_id: str, owner_key: str | None = None) -> str:
        if not owner_key:
            return f"transcript_{task_id}"
        owner_hash = hashlib.sha256(owner_key.encode()).hexdigest()[:16]
        return f"transcript_{owner_hash}_{task_id}"

    @property
    def indexed_count(self) -> int:
        """Number of indexed transcripts."""
        return self._store.count
