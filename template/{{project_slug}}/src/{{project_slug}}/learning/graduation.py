"""
GraduationEngine -- Promotes stable patterns from project-level to global-level.

When a preference or trust score has been consistent across N sessions,
the engine suggests graduating it to the global profile so it applies
to all future projects.

Uses a pluggable GraduationRule protocol. Ships with one built-in rule:
  - ConsistencyRule: promotes preferences stable across N sessions

Projects add domain-specific rules by implementing the protocol.
Graduation ALWAYS requires user confirmation via CheckInManager.

Keep this file under 200 lines.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .checkin_manager import CheckInManager
from .global_profile import GlobalProfileManager
from .schema import DEFAULT_DB_PATH, get_connection

logger = logging.getLogger(__name__)

DEFAULT_CONSISTENCY_THRESHOLD = 5


# =============================================================================
# GRADUATION RULE PROTOCOL
# =============================================================================


@runtime_checkable
class GraduationRule(Protocol):
    """
    Protocol for graduation rules. Projects implement this to add
    domain-specific graduation logic.

    A rule examines project data and returns candidates for graduation.
    """

    @property
    def name(self) -> str:
        """Human-readable rule name."""
        ...

    def find_candidates(
        self, project_id: str, db_path: Path
    ) -> list["GraduationCandidate"]:
        """Find preferences that should be considered for graduation."""
        ...


@dataclass
class GraduationCandidate:
    """A preference or pattern that's a candidate for global promotion."""

    key: str
    value: str
    source_project: str
    rule_name: str
    confidence: float = 0.5
    evidence: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# BUILT-IN RULE: CONSISTENCY
# =============================================================================


class ConsistencyRule:
    """
    Promotes preferences that have been stable across N sessions.

    A preference is "consistent" if:
      - It has been active for at least N sessions
      - Its value hasn't changed
      - Its priority is above a minimum threshold
    """

    def __init__(
        self,
        min_sessions: int = DEFAULT_CONSISTENCY_THRESHOLD,
        min_priority: int = 60,
    ):
        self._min_sessions = min_sessions
        self._min_priority = min_priority

    @property
    def name(self) -> str:
        return "consistency"

    def find_candidates(
        self, project_id: str, db_path: Path
    ) -> list[GraduationCandidate]:
        """Find preferences that have been stable across multiple sessions."""
        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                """SELECT key, value, source, priority, created_at, updated_at
                   FROM user_preferences
                   WHERE project_id = ? AND active = 1 AND priority >= ?
                   ORDER BY priority DESC""",
                (project_id, self._min_priority),
            ).fetchall()

            signal_count_row = conn.execute(
                """SELECT COUNT(DISTINCT session_id) as sessions
                   FROM feedback_signals
                   WHERE project_id = ? AND session_id != ''""",
                (project_id,),
            ).fetchone()
            total_sessions = signal_count_row["sessions"] if signal_count_row else 0

        finally:
            conn.close()

        if total_sessions < self._min_sessions:
            return []

        candidates = []
        for row in rows:
            data = dict(row)
            if data.get("created_at") == data.get("updated_at"):
                confidence = min(total_sessions / (self._min_sessions * 2), 1.0)
                candidates.append(GraduationCandidate(
                    key=data["key"],
                    value=data["value"],
                    source_project=project_id,
                    rule_name=self.name,
                    confidence=confidence,
                    evidence=(
                        f"Stable across {total_sessions} sessions, "
                        f"priority {data.get('priority', 0)}, "
                        f"source: {data.get('source', 'unknown')}"
                    ),
                ))

        return candidates


# =============================================================================
# GRADUATION ENGINE
# =============================================================================


class GraduationEngine:
    """
    Finds and applies graduation candidates using pluggable rules.

    Usage:
        engine = GraduationEngine(project_id="my_project")

        # Add custom rules
        engine.add_rule(MyDomainRule())

        # Find candidates
        candidates = engine.find_all_candidates()

        # Apply (creates check-in for user confirmation)
        for candidate in candidates:
            engine.propose_graduation(candidate)
    """

    def __init__(
        self,
        project_id: str = "default",
        db_path: Path = DEFAULT_DB_PATH,
        checkin_manager: CheckInManager | None = None,
        global_profile: GlobalProfileManager | None = None,
    ):
        self._project_id = project_id
        self._db_path = db_path
        self._checkin = checkin_manager or CheckInManager(db_path)
        self._global = global_profile or GlobalProfileManager()
        self._rules: list[GraduationRule] = [ConsistencyRule()]

    def add_rule(self, rule: GraduationRule) -> None:
        """Add a custom graduation rule."""
        self._rules.append(rule)
        logger.info(f"[Graduation] Added rule: {rule.name}")

    def find_all_candidates(self) -> list[GraduationCandidate]:
        """Run all rules and collect candidates."""
        all_candidates = []
        for rule in self._rules:
            try:
                candidates = rule.find_candidates(self._project_id, self._db_path)
                all_candidates.extend(candidates)
                logger.debug(
                    f"[Graduation] Rule '{rule.name}' found "
                    f"{len(candidates)} candidates"
                )
            except Exception as e:
                logger.error(f"[Graduation] Rule '{rule.name}' failed: {e}")
        return all_candidates

    def propose_graduation(self, candidate: GraduationCandidate) -> str:
        """
        Create a check-in to ask the user about graduating a preference.

        Returns the check-in ID.
        """
        checkin = self._checkin.create(
            checkin_type="graduation",
            prompt=(
                f"The preference '{candidate.key} = {candidate.value}' has been "
                f"consistent in this project. Apply it to all your future projects?"
            ),
            suggested_action=(
                f"Add to global profile: {candidate.key} = {candidate.value}"
            ),
            project_id=self._project_id,
            context={
                "candidate_key": candidate.key,
                "candidate_value": candidate.value,
                "rule": candidate.rule_name,
                "confidence": candidate.confidence,
                "evidence": candidate.evidence,
            },
        )
        logger.info(
            f"[Graduation] Proposed: {candidate.key}={candidate.value} "
            f"(check-in {checkin.id})"
        )
        return checkin.id

    def apply_graduation(self, candidate: GraduationCandidate) -> None:
        """Apply a graduated preference to the global profile."""
        self._global.add_global_preference(
            key=candidate.key,
            value=candidate.value,
            source_project=candidate.source_project,
            confidence=candidate.confidence,
        )
        logger.info(
            f"[Graduation] Applied: {candidate.key}={candidate.value} "
            f"to global profile"
        )
