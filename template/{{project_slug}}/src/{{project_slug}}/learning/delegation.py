"""
Delegation records -- opt-in phase-derivation trail for dispatches.

HONEST SCOPE: this platform is hub-and-spoke -- agents never call each
other, so there are no agent-to-agent delegations to record. What CAN
be recorded is phase derivation: which upstream artifacts fed each
downstream dispatch. One row per gated dispatch, keyed by the task id:

  analyze   -- derived_from [] (nothing upstream; the task itself)
  challenge -- derived_from names the agents whose Phase 1 analyses
               the challenger consumed
  vote      -- derived_from ["synthesis"] (voters consume the synthesis)

Detect-only, default OFF (DELEGATION_RECORDS_ENABLED, lenient parse),
never enforced, never auto-analyzed. Writes are fire-and-forget:
record_dispatches schedules the blocking store I/O off the event loop
WITHOUT awaiting it and swallows every error, so recording can never
fail or slow a dispatch -- even a hung store only stalls the write
task, never the phase.

Keep this file under 200 lines.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

TOGGLE_ENV = "DELEGATION_RECORDS_ENABLED"

PHASE_ANALYZE = "analyze"
PHASE_CHALLENGE = "challenge"
PHASE_VOTE = "vote"


class DelegationRecorder:
    """Writes delegation_records rows. Every write is best-effort."""

    def __init__(self, store):
        self._store = store

    def record(
        self,
        correlation_id: str,
        phase: str,
        agent_id: str,
        derived_from: list[str],
        tenant_id: str = "default",
    ) -> None:
        """Record one dispatch. Fire-and-forget (errors logged, not raised)."""
        try:
            self._store.insert(
                "delegation_records",
                {
                    "id": str(uuid.uuid4())[:12],
                    "correlation_id": correlation_id,
                    "tenant_id": tenant_id,
                    "phase": phase,
                    "agent_id": agent_id,
                    "derived_from_json": json.dumps(list(derived_from), default=str),
                    "created_at": datetime.now().isoformat(),
                },
            )
        except Exception as exc:
            logger.warning(f"[Delegation] record() failed (ignored): {exc}")

    def record_many(
        self,
        correlation_id: str,
        phase: str,
        agent_ids: list[str],
        derived_from: list[str],
        tenant_id: str = "default",
    ) -> None:
        """One row per dispatched agent, same derivation for the batch."""
        for agent_id in agent_ids:
            self.record(correlation_id, phase, agent_id, derived_from, tenant_id)


# Strong references to in-flight delegation writes: a bare
# loop.create_task result can be garbage-collected before it runs.
_pending_writes: set = set()


def record_dispatches(
    recorder,
    task,
    phase: str,
    agent_ids: list[str],
    derived_from: list[str],
) -> None:
    """Schedule a phase's dispatch records without blocking the phase.

    No-op when the recorder is None (toggle off / no store) or nothing
    was dispatched. Fire-and-forget, mirroring the identity-flag
    pattern in orchestration/dispatch_helpers.py: inside a running
    event loop the blocking store I/O is scheduled off-loop
    (asyncio.to_thread) WITHOUT being awaited, so even a slow or hung
    store never delays the phase; sync library callers write inline.
    Store errors are swallowed inside record_many, and any scheduling
    failure is logged and swallowed here.
    """
    if recorder is None or not agent_ids:
        return
    # Snapshot the lists before crossing the thread boundary so a caller
    # reusing/mutating them after scheduling cannot corrupt the write.
    args = (
        getattr(task, "id", "") or "",
        phase,
        list(agent_ids),
        list(derived_from),
        getattr(task, "tenant_id", "default") or "default",
    )
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    try:
        if loop is None:
            recorder.record_many(*args)
            return
        pending = loop.create_task(asyncio.to_thread(recorder.record_many, *args))
        _pending_writes.add(pending)
        pending.add_done_callback(_pending_writes.discard)
    except Exception as exc:
        logger.warning(f"[Delegation] dispatch recording not scheduled (ignored): {exc}")


async def flush_pending_writes() -> None:
    """Await any scheduled delegation writes still in flight.

    Determinism helper for tests and graceful shutdown -- the runtime
    dispatch path never calls it.
    """
    pending = list(_pending_writes)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def create_delegation_recorder(store) -> DelegationRecorder | None:
    """Build a recorder when DELEGATION_RECORDS_ENABLED is truthy, else None.

    Opt-in wiring (default OFF): returning None keeps every dispatch
    path byte-identical to the unrecorded behavior. Degrades to None
    with a warning when no store is available.
    """
    if os.environ.get(TOGGLE_ENV, "").strip().lower() not in ("true", "1", "yes"):
        return None
    if store is None:
        logger.warning(
            "[Delegation] DELEGATION_RECORDS_ENABLED=true but no learning "
            "store is available -- delegation records degrade to a no-op"
        )
        return None
    return DelegationRecorder(store)
