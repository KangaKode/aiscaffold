"""
FeedbackTracker -- Records and queries user feedback signals.

Every time a user accepts, rejects, modifies, rates, or dismisses an agent
output, a FeedbackSignal is recorded. These signals feed into:
  - AgentTrustManager (trust scores per agent)
  - CheckInManager (trigger threshold-based check-ins)
  - UserProfileManager (infer preferences from patterns)

Signal types are universal: accept, reject, modify, rate, dismiss, escalate.
Context types are project-defined strings (e.g., "chat", "round_table").

Security:
  - Content field is sanitized before storage (size-limited, null bytes stripped)
  - Metadata is validated for size limits

Keep this file under 250 lines.
"""

import json
import logging
from pathlib import Path

from ..security.prompt_guard import sanitize_for_prompt
from ..security.validators import validate_dict_size
from .models import FeedbackSignal
from .schema import DEFAULT_DB_PATH, dict_from_row, get_connection, initialize_schema

logger = logging.getLogger(__name__)

MAX_CONTENT_LENGTH = 50_000
MAX_METADATA_BYTES = 100_000


class FeedbackTracker:
    """
    Records and queries feedback signals.

    Usage:
        tracker = FeedbackTracker()

        # Record a signal
        tracker.record(FeedbackSignal(
            signal_type="accept",
            context_type="chat",
            agent_id="code_analyst",
        ))

        # Query signals
        signals = tracker.get_signals(agent_id="code_analyst", limit=50)
        rates = tracker.get_acceptance_rates(project_id="default")
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self._db_path = db_path
        initialize_schema(db_path)

    def record(self, signal: FeedbackSignal) -> FeedbackSignal:
        """
        Record a feedback signal to the database.

        Sanitizes content and validates metadata before storage.
        Returns the signal with its generated ID.
        """
        if signal.content:
            signal.content = sanitize_for_prompt(
                signal.content, max_length=MAX_CONTENT_LENGTH
            )

        if signal.metadata:
            validate_dict_size(
                signal.metadata, "metadata", max_size_bytes=MAX_METADATA_BYTES
            )

        conn = get_connection(self._db_path)
        try:
            conn.execute(
                """INSERT INTO feedback_signals
                   (id, project_id, signal_type, context_type, agent_id,
                    content, confidence, metadata_json, session_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal.id,
                    signal.project_id,
                    signal.signal_type,
                    signal.context_type,
                    signal.agent_id,
                    signal.content,
                    signal.confidence,
                    json.dumps(signal.metadata, default=str),
                    signal.session_id,
                    signal.created_at,
                ),
            )
            conn.commit()
            logger.debug(
                f"[FeedbackTracker] Recorded {signal.signal_type} "
                f"for agent={signal.agent_id} context={signal.context_type}"
            )
            return signal
        finally:
            conn.close()

    def get_signals(
        self,
        project_id: str = "default",
        agent_id: str | None = None,
        signal_type: str | None = None,
        context_type: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[FeedbackSignal]:
        """Query feedback signals with optional filters."""
        query = "SELECT * FROM feedback_signals WHERE project_id = ?"
        params: list = [project_id]

        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if signal_type:
            query += " AND signal_type = ?"
            params.append(signal_type)
        if context_type:
            query += " AND context_type = ?"
            params.append(context_type)
        if since:
            query += " AND created_at >= ?"
            params.append(since)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_signal(dict_from_row(r)) for r in rows]
        finally:
            conn.close()

    def get_signal_counts(
        self,
        project_id: str = "default",
        agent_id: str | None = None,
        since: str | None = None,
    ) -> dict[str, int]:
        """Get counts by signal type. Returns {"accept": 10, "reject": 3, ...}."""
        query = """SELECT signal_type, COUNT(*) as count
                   FROM feedback_signals WHERE project_id = ?"""
        params: list = [project_id]

        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if since:
            query += " AND created_at >= ?"
            params.append(since)

        query += " GROUP BY signal_type"

        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(query, params).fetchall()
            return {row["signal_type"]: row["count"] for row in rows}
        finally:
            conn.close()

    def get_acceptance_rates(
        self,
        project_id: str = "default",
        since: str | None = None,
    ) -> dict[str, float]:
        """
        Get acceptance rate per agent.

        Returns {"agent_id": 0.75, ...} where 0.75 means 75% of signals
        were "accept" vs "reject"/"modify".
        """
        query = """SELECT agent_id, signal_type, COUNT(*) as count
                   FROM feedback_signals
                   WHERE project_id = ? AND agent_id != ''"""
        params: list = [project_id]

        if since:
            query += " AND created_at >= ?"
            params.append(since)

        query += " GROUP BY agent_id, signal_type"

        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(query, params).fetchall()

            agent_counts: dict[str, dict[str, int]] = {}
            for row in rows:
                aid = row["agent_id"]
                if aid not in agent_counts:
                    agent_counts[aid] = {"positive": 0, "total": 0}
                agent_counts[aid]["total"] += row["count"]
                if row["signal_type"] in ("accept", "rate"):
                    agent_counts[aid]["positive"] += row["count"]

            return {
                aid: counts["positive"] / max(counts["total"], 1)
                for aid, counts in agent_counts.items()
            }
        finally:
            conn.close()

    def get_total_count(self, project_id: str = "default") -> int:
        """Get total number of feedback signals for a project."""
        conn = get_connection(self._db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) as count FROM feedback_signals WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            return row["count"] if row else 0
        finally:
            conn.close()

    @staticmethod
    def _row_to_signal(data: dict) -> FeedbackSignal:
        """Convert a database row dict to a FeedbackSignal."""
        return FeedbackSignal(
            id=data["id"],
            project_id=data.get("project_id", "default"),
            signal_type=data["signal_type"],
            context_type=data.get("context_type", ""),
            agent_id=data.get("agent_id", ""),
            content=data.get("content", ""),
            confidence=data.get("confidence", 0.5),
            metadata=data.get("metadata", {}),
            session_id=data.get("session_id", ""),
            created_at=data.get("created_at", ""),
        )
