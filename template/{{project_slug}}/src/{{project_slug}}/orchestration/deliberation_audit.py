"""
Deliberation audit -- metadata-only audit trail for round table runs.

Records WHAT happened during multi-agent deliberation (phases, agent
counts, durations, outcomes) without recording WHAT WAS SAID. Events go
to the learning store's audit_events table, keyed by a correlation id
so a whole run can be reconstructed as a timeline.

No free text by construction: detail values are restricted to numbers,
booleans, and short enum-like strings (any string longer than
MAX_DETAIL_STR_CHARS is dropped with a warning). This keeps the audit
trail safe to retain and expose broadly -- it can never leak prompt or
response content, because it structurally cannot store it.

Auditing is fire-and-forget: a failing store write is logged, never
raised, so audit problems cannot break a deliberation.

Usage:
    auditor = DeliberationAuditor(store=get_learning_store())
    result = await audited_round_table(round_table, task, auditor)
    timeline = auditor.get_timeline(...)

audited_round_table() wraps a full run with deliberation_started /
deliberation_completed events. Projects that want per-phase granularity
can call auditor.event(...) with their own event_type/phase values from
inside their own orchestration extensions (subclasses, wrappers, or
callbacks) -- the schema is intentionally generic.

Keep this file under 250 lines.
"""

import json
import logging
import time
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

# Longest string allowed as a detail value. Long enough for enum-like
# labels ("no_consensus", "quorum_not_met"), far too short for prompt or
# response content.
MAX_DETAIL_STR_CHARS = 64

EVENT_STARTED = "deliberation_started"
EVENT_COMPLETED = "deliberation_completed"


def new_correlation_id() -> str:
    """Fresh correlation id for one deliberation run."""
    return uuid.uuid4().hex[:16]


def _clean_detail(detail: dict) -> dict:
    """
    Keep only metadata-shaped values: int/float/bool/None and strings
    up to MAX_DETAIL_STR_CHARS. Everything else (long strings, nested
    structures, arbitrary objects) is dropped with a warning -- this is
    what enforces "no free text" by construction.
    """
    cleaned: dict = {}
    for key, value in detail.items():
        if isinstance(value, bool | int | float) or value is None:
            cleaned[key] = value
        elif isinstance(value, str) and len(value) <= MAX_DETAIL_STR_CHARS:
            cleaned[key] = value
        else:
            logger.warning(
                f"[DeliberationAudit] Dropping detail '{key}': value must be a "
                f"number, bool, or string <= {MAX_DETAIL_STR_CHARS} chars"
            )
    return cleaned


class DeliberationAuditor:
    """
    Writes metadata-only audit events to the audit_events table.

    The store is optional: without one, event() is a logged no-op and
    get_timeline() returns [] -- callers never need to branch.
    """

    def __init__(self, store=None):
        self._store = store

    def event(
        self,
        correlation_id: str,
        event_type: str,
        tenant_id: str = "default",
        phase: str = "",
        agent_count: int = 0,
        duration_seconds: float = 0.0,
        outcome: str = "",
        **detail,
    ) -> None:
        """
        Record one audit event. Fire-and-forget: exceptions are logged,
        never raised. Extra keyword arguments become detail_json after
        filtering (see _clean_detail -- no free text).
        """
        if self._store is None:
            logger.debug("[DeliberationAudit] No store configured; event dropped")
            return
        try:
            self._store.insert(
                "audit_events",
                {
                    "id": str(uuid.uuid4())[:12],
                    "correlation_id": correlation_id,
                    "event_type": event_type,
                    "tenant_id": tenant_id,
                    "phase": phase,
                    "agent_count": int(agent_count),
                    "duration_seconds": float(duration_seconds),
                    "outcome": outcome[:MAX_DETAIL_STR_CHARS],
                    "detail_json": json.dumps(_clean_detail(detail), default=str),
                    "created_at": datetime.now().isoformat(),
                },
            )
        except Exception as exc:
            logger.warning(f"[DeliberationAudit] Failed to record event: {exc}")

    def get_timeline(self, correlation_id: str) -> list[dict]:
        """All events for one run, ordered by created_at (oldest first)."""
        if self._store is None:
            return []
        return self._store.query(
            "audit_events",
            {"correlation_id": correlation_id},
            order_by="created_at",
        )


async def audited_round_table(
    round_table,
    task,
    auditor: DeliberationAuditor,
    tenant_id: str = "default",
    correlation_id: str = "",
):
    """
    Run a RoundTable deliberation with start/completion audit events.

    Standalone wrapper -- the round table itself is untouched. Emits
    deliberation_started, awaits round_table.run(task), then emits
    deliberation_completed with the run's agent count, duration, and
    outcome (plus approval_rate/degraded detail when present). If the
    run raises, a completion event with outcome "failed" is still
    emitted before the exception propagates.

    Pass a correlation_id to control the timeline key (e.g. so an API
    layer can hand it back to the caller); otherwise a fresh one is
    generated. Returns whatever round_table.run() returns.
    """
    correlation_id = correlation_id or new_correlation_id()
    agent_count = len(getattr(round_table, "agents", []) or [])
    auditor.event(
        correlation_id,
        EVENT_STARTED,
        tenant_id=tenant_id,
        agent_count=agent_count,
    )

    started = time.monotonic()
    try:
        result = await round_table.run(task)
    except Exception:
        auditor.event(
            correlation_id,
            EVENT_COMPLETED,
            tenant_id=tenant_id,
            agent_count=agent_count,
            duration_seconds=time.monotonic() - started,
            outcome="failed",
        )
        raise

    consensus = getattr(result, "consensus_reached", None)
    outcome = "consensus" if consensus else "no_consensus"
    auditor.event(
        correlation_id,
        EVENT_COMPLETED,
        tenant_id=tenant_id,
        agent_count=agent_count,
        duration_seconds=getattr(
            result, "duration_seconds", time.monotonic() - started
        ),
        outcome=outcome,
        approval_rate=getattr(result, "approval_rate", None),
        degraded=getattr(result, "degraded", None),
        failed_agent_count=getattr(result, "failed_agent_count", None),
    )
    return result
