"""
AgentTrustManager -- EMA-based trust scores per agent.

Trust scores start at 0.5 (neutral) and adjust based on feedback signals:
  - "accept" increases trust
  - "reject" decreases trust
  - "modify" slightly decreases trust (output needed correction)
  - "rate" adjusts proportionally to the rating value
  - "escalate" slightly decreases trust (agent couldn't handle it)

Uses Exponential Moving Average (EMA) so recent signals matter more than
old ones. Configurable decay rate, floor (0.1), and ceiling (0.95).

The ChatOrchestrator uses trust scores to prefer higher-trust agents.
The RoundTable can weight synthesis by trust.

Keep this file under 200 lines.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from .models import AgentTrustScore, FeedbackSignal, SignalType
from .schema import DEFAULT_DB_PATH, dict_from_row, get_connection, initialize_schema

logger = logging.getLogger(__name__)

DEFAULT_TRUST = 0.5
TRUST_FLOOR = 0.1
TRUST_CEILING = 0.95
EMA_ALPHA = 0.15

SIGNAL_TARGETS = {
    SignalType.ACCEPT: 0.9,
    SignalType.REJECT: 0.15,
    SignalType.MODIFY: 0.35,
    SignalType.DISMISS: 0.3,
    SignalType.ESCALATE: 0.25,
}


class AgentTrustManager:
    """
    Manages trust scores for agents based on feedback signals.

    Usage:
        trust_mgr = AgentTrustManager()

        # Update from a feedback signal
        trust_mgr.update_from_signal(signal)

        # Get trust score
        score = trust_mgr.get_trust("code_analyst")  # -> 0.72

        # Get all scores (for routing)
        scores = trust_mgr.get_all_scores()  # -> {"code_analyst": 0.72, ...}
    """

    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        ema_alpha: float = EMA_ALPHA,
        trust_floor: float = TRUST_FLOOR,
        trust_ceiling: float = TRUST_CEILING,
    ):
        self._db_path = db_path
        self._alpha = ema_alpha
        self._floor = trust_floor
        self._ceiling = trust_ceiling
        initialize_schema(db_path)

    def update_from_signal(self, signal: FeedbackSignal) -> AgentTrustScore:
        """
        Update an agent's trust score based on a feedback signal.

        Proper EMA: new_score = alpha * target + (1 - alpha) * current
        Where target is the signal-implied trust level.
        Clamped to [floor, ceiling].
        """
        if not signal.agent_id:
            return AgentTrustScore(agent_id="", project_id=signal.project_id)

        current = self.get_trust_entry(signal.agent_id, signal.project_id)

        if signal.signal_type == SignalType.RATE and signal.confidence:
            target = signal.confidence
        else:
            target = SIGNAL_TARGETS.get(signal.signal_type, current.trust_score)

        new_score = self._alpha * target + (1 - self._alpha) * current.trust_score
        new_score = max(self._floor, min(self._ceiling, new_score))

        positive = signal.signal_type in (SignalType.ACCEPT, SignalType.RATE)
        new_count = current.interaction_count + 1
        new_rate = (
            (current.acceptance_rate * current.interaction_count + (1.0 if positive else 0.0))
            / new_count
        )

        conn = get_connection(self._db_path)
        try:
            conn.execute(
                """INSERT INTO agent_trust
                   (agent_id, project_id, trust_score, interaction_count,
                    acceptance_rate, last_signal_type, metadata_json, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project_id, agent_id) DO UPDATE SET
                    trust_score = excluded.trust_score,
                    interaction_count = excluded.interaction_count,
                    acceptance_rate = excluded.acceptance_rate,
                    last_signal_type = excluded.last_signal_type,
                    last_updated = excluded.last_updated""",
                (
                    signal.agent_id,
                    signal.project_id,
                    new_score,
                    new_count,
                    new_rate,
                    signal.signal_type,
                    json.dumps(current.metadata, default=str),
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()

            logger.debug(
                f"[AgentTrust] {signal.agent_id}: "
                f"{current.trust_score:.3f} -> {new_score:.3f} "
                f"({signal.signal_type}, count={new_count})"
            )

            return AgentTrustScore(
                agent_id=signal.agent_id,
                project_id=signal.project_id,
                trust_score=new_score,
                interaction_count=new_count,
                acceptance_rate=new_rate,
                last_signal_type=signal.signal_type,
                last_updated=datetime.now().isoformat(),
            )
        finally:
            conn.close()

    def get_trust(self, agent_id: str, project_id: str = "default") -> float:
        """Get trust score for an agent. Returns DEFAULT_TRUST if not found."""
        entry = self.get_trust_entry(agent_id, project_id)
        return entry.trust_score

    def get_trust_entry(
        self, agent_id: str, project_id: str = "default"
    ) -> AgentTrustScore:
        """Get full trust entry for an agent."""
        conn = get_connection(self._db_path)
        try:
            row = conn.execute(
                "SELECT * FROM agent_trust WHERE project_id = ? AND agent_id = ?",
                (project_id, agent_id),
            ).fetchone()

            if row is None:
                return AgentTrustScore(
                    agent_id=agent_id, project_id=project_id,
                    trust_score=DEFAULT_TRUST,
                )

            data = dict_from_row(row)
            return AgentTrustScore(
                agent_id=data["agent_id"],
                project_id=data.get("project_id", project_id),
                trust_score=data.get("trust_score", DEFAULT_TRUST),
                interaction_count=data.get("interaction_count", 0),
                acceptance_rate=data.get("acceptance_rate", 0.5),
                last_signal_type=data.get("last_signal_type", ""),
                metadata=data.get("metadata", {}),
                last_updated=data.get("last_updated", ""),
            )
        finally:
            conn.close()

    def get_all_scores(self, project_id: str = "default") -> dict[str, float]:
        """Get all trust scores for a project. Returns {agent_id: score}."""
        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(
                "SELECT agent_id, trust_score FROM agent_trust WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            return {row["agent_id"]: row["trust_score"] for row in rows}
        finally:
            conn.close()

    def get_all_entries(self, project_id: str = "default") -> list[AgentTrustScore]:
        """Get all trust entries for a project."""
        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM agent_trust WHERE project_id = ? ORDER BY trust_score DESC",
                (project_id,),
            ).fetchall()
            return [
                AgentTrustScore(
                    agent_id=dict_from_row(r)["agent_id"],
                    project_id=dict_from_row(r).get("project_id", project_id),
                    trust_score=dict_from_row(r).get("trust_score", DEFAULT_TRUST),
                    interaction_count=dict_from_row(r).get("interaction_count", 0),
                    acceptance_rate=dict_from_row(r).get("acceptance_rate", 0.5),
                    last_signal_type=dict_from_row(r).get("last_signal_type", ""),
                    metadata=dict_from_row(r).get("metadata", {}),
                    last_updated=dict_from_row(r).get("last_updated", ""),
                )
                for r in rows
            ]
        finally:
            conn.close()
