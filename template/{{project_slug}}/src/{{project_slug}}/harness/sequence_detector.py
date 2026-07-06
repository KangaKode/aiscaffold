"""
Sequence detector -- multi-step extraction-playbook detection.

PR-4's activity thresholds catch *volume* anomalies (too many requests in
an hour). They miss the patient attacker who stays under every rate limit
but walks a deliberate sequence: search knowledge, pull the corrections
that back it, then export. This detector catches the *shape* of that
playbook rather than its volume.

It reads a user's recent activity_events and backward-chains from the
terminal step of each pattern. Backward-chaining (anchoring on the most
recent terminal action and walking earlier) resists timestamp poisoning:
an attacker cannot hide a step by back-dating an event, because the match
is anchored on the newest action and only accepts earlier steps before
each anchor.

Steps are matched by route substring + HTTP method, so patterns are
expressed in terms of your own API surface -- no separate activity-type
taxonomy to maintain. Matches are written to integrity_flags for human
review; nothing is blocked automatically.

Keep this file under 300 lines. (Raised from 250: the detector set
shipped slightly over its original budget; new detectors go in a
sibling module.)
"""

import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

FLAG_TYPE_SEQUENCE = "extraction_sequence"

# Cap on how many recent events one check reads (mirrors activity.py).
WINDOW_FETCH_CAP = 2000


@dataclass(frozen=True)
class Step:
    """One step in an extraction pattern, matched against an activity row.

    route_contains: substring that must appear in the row's route.
    method: HTTP method that must match (empty = any).
    """

    route_contains: str
    method: str = ""

    def matches(self, row: dict) -> bool:
        if self.route_contains not in row.get("route", ""):
            return False
        if self.method and row.get("method", "").upper() != self.method.upper():
            return False
        return True


@dataclass(frozen=True)
class ExtractionPattern:
    """An ordered sequence of steps that must occur within a time window.

    Validated at construction: fewer than two steps would match every
    occurrence of a single action (alert storms), and a non-positive
    window disables the time bound.
    """

    name: str
    steps: tuple[Step, ...]
    max_window_seconds: float
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ExtractionPattern.name must be non-empty")
        if len(self.steps) < 2:
            raise ValueError(
                f"ExtractionPattern '{self.name}': need >= 2 steps, got {len(self.steps)}"
            )
        if not math.isfinite(self.max_window_seconds) or self.max_window_seconds <= 0:
            raise ValueError(
                f"ExtractionPattern '{self.name}': max_window_seconds must be positive"
            )


# Default patterns are expressed against the scaffold's own routes. Replace
# or extend these for your API surface -- they are examples, not a complete
# threat model.
DEFAULT_PATTERNS: tuple[ExtractionPattern, ...] = (
    ExtractionPattern(
        name="knowledge_extraction",
        steps=(
            Step("/chat", "POST"),
            Step("/corrections", "GET"),
            Step("/sessions", "GET"),
        ),
        max_window_seconds=300,
        description="Query -> pull backing corrections -> export session data",
    ),
    ExtractionPattern(
        name="rogue_agent_injection",
        steps=(
            Step("/agents", "POST"),
            Step("/round-table", "POST"),
        ),
        max_window_seconds=600,
        description="Register an agent -> immediately drive deliberations with it",
    ),
    ExtractionPattern(
        name="credential_churn_burst",
        steps=(
            Step("/credentials", "POST"),
            Step("/chat", "POST"),
        ),
        max_window_seconds=120,
        description="Rotate credentials -> immediate burst of queries",
    ),
)


@dataclass
class SequenceMatch:
    """A detected extraction sequence for one user."""

    pattern_name: str
    user_id: str
    window_seconds: float
    step_times: list[str] = field(default_factory=list)


class SequenceDetector:
    """Stateless backward-chaining detector over activity_events.

    Reads a user's recent events and checks each pattern. Does not mutate
    the store's rows. Matches are persisted as integrity_flags and also
    returned so callers can act on them synchronously if they want.
    """

    def __init__(self, store, patterns: tuple[ExtractionPattern, ...] | None = None):
        self._store = store
        self._patterns = patterns if patterns is not None else DEFAULT_PATTERNS

    def check(
        self,
        user_id: str,
        tenant_id: str = "default",
        now: datetime | None = None,
    ) -> list[SequenceMatch]:
        """Check a user's recent activity for any extraction pattern.

        Returns every pattern that matched (usually zero). Each match is
        also written to integrity_flags. now is injectable for testing.
        """
        now = now or datetime.now()
        try:
            rows = self._store.query(
                "activity_events",
                {"tenant_id": tenant_id, "user_id": user_id},
                order_by="created_at DESC",
                limit=WINDOW_FETCH_CAP,
            )
        except Exception as exc:
            logger.warning(f"[Sequence] activity query failed: {exc}")
            return []

        matches: list[SequenceMatch] = []
        for pattern in self._patterns:
            match = self._match_pattern(pattern, rows, user_id)
            if match is not None:
                matches.append(match)
                self._flag(match, tenant_id)
        return matches

    def _match_pattern(
        self, pattern: ExtractionPattern, rows: list[dict], user_id: str
    ) -> SequenceMatch | None:
        """Backward-chain one pattern over the user's rows.

        rows are newest-first. We anchor on the most recent event matching
        the terminal step, then require each earlier step to have an event
        strictly before the running anchor. The whole chain must fit inside
        max_window_seconds.
        """
        terminal = pattern.steps[-1]
        terminal_row = next((r for r in rows if terminal.matches(r)), None)
        if terminal_row is None:
            return None

        anchor = _parse_ts(terminal_row.get("created_at", ""))
        if anchor is None:
            return None

        step_times = [anchor]
        for step in reversed(pattern.steps[:-1]):
            prior = _latest_before(rows, step, anchor)
            if prior is None:
                return None
            step_times.append(prior)
            anchor = prior

        step_times.reverse()
        elapsed = (step_times[-1] - step_times[0]).total_seconds()
        if elapsed > pattern.max_window_seconds:
            return None

        return SequenceMatch(
            pattern_name=pattern.name,
            user_id=user_id,
            window_seconds=elapsed,
            step_times=[t.isoformat() for t in step_times],
        )

    def _flag(self, match: SequenceMatch, tenant_id: str) -> None:
        """Persist a sequence match as an integrity flag (best-effort)."""
        try:
            self._store.insert(
                "integrity_flags",
                {
                    "id": str(uuid.uuid4())[:12],
                    "flag_type": FLAG_TYPE_SEQUENCE,
                    "subject_id": match.user_id,
                    "tenant_id": tenant_id,
                    "severity": "warning",
                    "detail_json": json.dumps(
                        {
                            "pattern": match.pattern_name,
                            "window_seconds": match.window_seconds,
                            "step_times": match.step_times,
                        },
                        default=str,
                    ),
                    "created_at": datetime.now().isoformat(),
                    "resolved": 0,
                },
            )
            logger.warning(
                f"[Sequence] User '{match.user_id}' matched extraction "
                f"pattern '{match.pattern_name}' in {match.window_seconds:.0f}s"
            )
        except Exception as exc:
            logger.error(f"[Sequence] Failed to persist flag: {exc}")


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _latest_before(rows: list[dict], step: Step, anchor: datetime) -> datetime | None:
    """Most recent event matching step with a timestamp strictly < anchor."""
    best: datetime | None = None
    for row in rows:
        if not step.matches(row):
            continue
        ts = _parse_ts(row.get("created_at", ""))
        if ts is None or ts >= anchor:
            continue
        if best is None or ts > best:
            best = ts
    return best
