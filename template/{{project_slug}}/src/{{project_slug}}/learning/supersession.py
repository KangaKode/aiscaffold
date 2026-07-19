"""
Human-gated supersession of corrections.

A successor row replaces an ancestor: ancestor keeps status=approved but
gets invalid_at set and drops out of prompt grounding. No auto-invalidate.
Concurrency uses update_if + compensating path (no multi-statement txn).

update_if is REQUIRED (unlike lifecycle.transition). Stores without it
raise MissingUpdateIfError before any status change; API maps to 501.

Keep this file under 170 lines.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from .flags import insert_flag_once

logger = logging.getLogger(__name__)

PARTIAL_FAILURE_FLAG = "supersession_partial_failure"
SUPERSEDED_EVENT = "correction_superseded"


class MissingUpdateIfError(RuntimeError):
    """Store lacks update_if; supersession cannot run safely."""


class SupersessionConflictError(ValueError):
    """Ancestor race lost or compensator fired; API maps to 409."""


def require_update_if(store: Any):
    """Return store.update_if or raise MissingUpdateIfError (no fallback)."""
    update_if = getattr(store, "update_if", None)
    if not callable(update_if):
        raise MissingUpdateIfError(
            f"Store {type(store).__name__!r} has no update_if(); "
            "supersession approve requires a conditional UPDATE primitive"
        )
    return update_if


def finalize_supersession_approve(
    store: Any,
    *,
    successor_id: str,
    ancestor_id: str,
    tenant_id: str,
    approved_by: str,
    now: str,
) -> None:
    """After successor is approved: invalidate ancestor or compensate."""
    update_if = require_update_if(store)
    expected = {"status": "approved", "invalid_at": "", "tenant_id": tenant_id}
    try:
        won = update_if(
            "corrections", ancestor_id, {"invalid_at": now}, expected
        )
    except Exception as exc:
        _compensate(
            store, update_if, successor_id, tenant_id, now, ancestor_id,
            f"ancestor_update_raised:{exc}",
        )
        raise SupersessionConflictError(
            f"Failed to invalidate ancestor {ancestor_id}: {exc}"
        ) from exc
    if not won:
        _compensate(
            store, update_if, successor_id, tenant_id, now, ancestor_id,
            "ancestor_no_longer_eligible",
        )
        raise SupersessionConflictError(
            f"Ancestor {ancestor_id} is no longer eligible for supersession"
        )
    _record_superseded_event(
        store, ancestor_id, successor_id, tenant_id, approved_by
    )


def _compensate(
    store, update_if, successor_id, tenant_id, now, ancestor_id, reason
) -> None:
    try:
        demoted = update_if(
            "corrections", successor_id, {"invalid_at": now},
            {"status": "approved", "invalid_at": "", "tenant_id": tenant_id},
        )
        if not demoted:
            logger.critical(
                "[Supersession] Compensator update_if matched zero rows "
                f"(ancestor={ancestor_id} successor={successor_id} "
                f"reason={reason}); successor may still be currently valid"
            )
        insert_flag_once(
            store,
            flag_type=PARTIAL_FAILURE_FLAG,
            subject_id=successor_id,
            tenant_id=tenant_id,
            detail={
                "ancestor_id": ancestor_id,
                "successor_id": successor_id,
                "reason": reason,
                "successor_demoted": bool(demoted),
            },
            severity="error",
        )
    except Exception as exc:
        logger.critical(
            "[Supersession] Compensator failed "
            f"(ancestor={ancestor_id} successor={successor_id}): {exc}"
        )


def _record_superseded_event(
    store, ancestor_id, successor_id, tenant_id, approved_by
) -> None:
    try:
        store.insert(
            "audit_events",
            {
                "id": str(uuid.uuid4())[:12],
                "correlation_id": f"supersede-{successor_id}",
                "event_type": SUPERSEDED_EVENT,
                "tenant_id": tenant_id,
                "outcome": "superseded",
                "detail_json": json.dumps({
                    "ancestor_id": ancestor_id,
                    "successor_id": successor_id,
                }),
                "created_at": datetime.now().isoformat(),
                "user_id": (approved_by or "")[:64],
            },
        )
    except Exception as exc:
        logger.warning(f"[Supersession] Audit event write failed: {exc}")


def list_successor_ids(store, correction_id: str, tenant_id: str) -> list[str]:
    """Ids of rows that supersede correction_id in this tenant."""
    rows = store.query(
        "corrections",
        {"supersedes_id": correction_id, "tenant_id": tenant_id},
        limit=50,
    )
    return [r["id"] for r in rows]


def validate_ancestor_for_supersession(ancestor, tenant_id: str) -> None:
    """Raise ValueError unless ancestor is currently-valid approved."""
    if ancestor is None or ancestor.tenant_id != tenant_id:
        raise ValueError("Correction not found")
    if ancestor.status != "approved" or ancestor.invalid_at:
        raise ValueError(
            f"Correction {ancestor.id} is not currently-valid approved "
            "knowledge and cannot be superseded"
        )
