"""
TranscriptIndexer -- Semantic search over round table deliberations.

Indexes completed round table results into the VectorStore so past
deliberations can be retrieved by semantic search. Follows the same
pattern as PreferenceRetriever.

Usage:
    indexer = TranscriptIndexer()
    indexer.index_result(round_table_result, task_content="Analyze API design")
    results = indexer.search("authentication best practices", limit=5)

Keep this file under 200 lines.
"""

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

    def index_result(self, result: Any, task_content: str = "") -> None:
        """Index a round table result for semantic search.

        Combines task content, agent analyses, synthesis recommendation,
        and vote outcomes into a searchable document.

        Args:
            result: A RoundTableResult (imported lazily to avoid circular deps).
            task_content: The original task text submitted by the user.
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

        embedding_result = self._embedder.embed(doc_text)

        self._store.add(
            doc_id=f"transcript_{task_id}",
            content=doc_text,
            metadata={
                "task_id": task_id,
                "agent_names": agent_names,
                "consensus_reached": str(consensus),
                "approval_rate": str(round(approval, 2)),
                "duration_seconds": str(round(duration, 2)),
                "timestamp": datetime.now().isoformat(),
                "doc_type": "round_table_transcript",
            },
            embedding=embedding_result.embedding,
        )
        logger.debug(f"[TranscriptIndexer] Indexed transcript for task {task_id}")

    def search(
        self,
        query: str,
        limit: int = 10,
        consensus_only: bool = False,
    ) -> SearchResults:
        """Semantic search over past round table results.

        Args:
            query: Natural language search query.
            limit: Maximum number of results.
            consensus_only: If True, only return results where consensus was reached.
        """
        embedding_result = self._embedder.embed(query)

        results = self._store.search(
            query=query,
            limit=limit,
            query_embedding=embedding_result.embedding,
        )

        if consensus_only:
            results.results = [
                r for r in results.results
                if r.metadata.get("consensus_reached") == "True"
            ]
            results.total = len(results.results)

        return results

    def get_by_task_id(self, task_id: str) -> SearchResult | None:
        """Direct lookup of a transcript by task ID."""
        results = self._store.search(
            query=task_id,
            limit=50,
        )
        doc_id = f"transcript_{task_id}"
        for r in results.results:
            if r.id == doc_id:
                return r
        return None

    @property
    def indexed_count(self) -> int:
        """Number of indexed transcripts."""
        return self._store.count
