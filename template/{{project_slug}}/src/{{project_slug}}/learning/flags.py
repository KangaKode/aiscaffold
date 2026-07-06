"""
Integrity-flag helpers shared by the detection modules.

The detection modules (timing_analysis, extraction_guard,
approval_patterns, activity) all persist findings the same way: one
integrity_flags row per finding, deduplicated by a persistence-level
cooldown -- before inserting, the store is queried for UNRESOLVED flags
of the same type and subject. The insert is skipped only when an
existing unresolved flag's severity is at or above the new one, so a
condition that WORSENS (warning -> error) still surfaces; only
same-or-lower repeats are suppressed. A human resolving the flags
(POST /activity/anomalies/{id}/resolve) re-arms detection for that
subject.

record_flag_hit is the insert-or-update variant for high-frequency
detectors: instead of silently suppressing repeats behind the cooldown,
it updates the unresolved flag in place (bounded hit count + last_seen +
latest finding detail) and escalates severity to "error" after a
configurable number of repeats -- so a sustained campaign cannot hide
behind one stale warning armed by a decoy probe.

Leaf module: imports stdlib only; the store is passed in.

Keep this file under 200 lines.
"""

import json
import logging
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

# Severity ordering for cooldown comparisons. Unknown severities rank
# as "warning".
SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}


def _rank(severity: str) -> int:
    return SEVERITY_RANK.get(severity, SEVERITY_RANK["warning"])


def max_unresolved_severity(
    store, flag_type: str, subject_id: str, tenant_id: str = "default"
) -> str | None:
    """Highest severity among unresolved flags of this type+subject, or
    None when no unresolved flag exists."""
    try:
        rows = store.query(
            "integrity_flags",
            {
                "flag_type": flag_type,
                "subject_id": subject_id,
                "tenant_id": tenant_id,
                "resolved": 0,
            },
        )
    except Exception as exc:
        logger.warning(f"[Flags] Cooldown query failed (treating as flagged): {exc}")
        # Fail toward "already flagged at max severity" so a broken store
        # cannot cause unbounded duplicate inserts.
        return "error"
    if not rows:
        return None
    return max((r.get("severity") or "warning" for r in rows), key=_rank)


def insert_flag_once(
    store,
    flag_type: str,
    subject_id: str,
    tenant_id: str,
    detail: dict,
    severity: str = "warning",
) -> bool:
    """
    Insert an integrity flag unless an unresolved one of the same
    type+subject already exists at the same or higher severity
    (persistence-level cooldown that never suppresses an escalation).
    Check-then-insert is not atomic: concurrent evaluators can slip
    past the cooldown and insert duplicates (acceptable single-node;
    multi-worker gets occasional extra flags, never missing ones).
    Best-effort: storage errors are logged, never raised. Returns True
    if inserted.
    """
    existing = max_unresolved_severity(store, flag_type, subject_id, tenant_id)
    if existing is not None and _rank(existing) >= _rank(severity):
        return False
    try:
        store.insert(
            "integrity_flags",
            {
                "id": str(uuid.uuid4())[:12],
                "flag_type": flag_type,
                "subject_id": subject_id,
                "tenant_id": tenant_id,
                "severity": severity,
                "detail_json": json.dumps(detail, default=str),
                "created_at": datetime.now().isoformat(),
                "resolved": 0,
            },
        )
        return True
    except Exception as exc:
        logger.error(f"[Flags] Failed to persist {flag_type} flag: {exc}")
        return False


def record_flag_hit(
    store,
    flag_type: str,
    subject_id: str,
    tenant_id: str,
    detail: dict,
    severity: str = "warning",
    escalate_after: int = 10,
) -> bool:
    """
    Insert-or-update: first hit inserts an integrity flag; while an
    unresolved flag of the same type+subject+tenant exists, later hits
    UPDATE it in place instead of being silently suppressed -- the
    detail is replaced (bounded: the caller's latest detail plus a hit
    counter and last_seen timestamp), and once the counter reaches
    ``escalate_after`` the severity is raised to "error" so a sustained
    campaign surfaces even though the first flag is still unresolved.
    Read-then-write is not atomic: concurrent evaluators can miss each
    other's counter bumps (acceptable single-node; multi-worker
    undercounts occasionally, never loses the flag). Best-effort like
    insert_flag_once: storage errors are logged, never raised.
    Returns True when a new row was inserted.
    """
    def _unresolved_rows():
        return store.query(
            "integrity_flags",
            {
                "flag_type": flag_type,
                "subject_id": subject_id,
                "tenant_id": tenant_id,
                "resolved": 0,
            },
        )

    try:
        rows = _unresolved_rows()
    except Exception as exc:
        # Fail toward "already flagged" so a broken store cannot cause
        # unbounded duplicate inserts (same posture as the cooldown).
        logger.warning(f"[Flags] Hit-record query failed (treating as flagged): {exc}")
        return False

    now = datetime.now().isoformat()
    if not rows:
        if insert_flag_once(
            store, flag_type, subject_id, tenant_id,
            {**detail, "hits": 1, "last_seen": now}, severity,
        ):
            return True
        # Lost the insert race: a concurrent scan created the flag
        # between our read and the insert's own cooldown check. Re-read
        # and fall through to the update path so this hit still lands
        # in the counter instead of being silently dropped.
        try:
            rows = _unresolved_rows()
        except Exception as exc:
            logger.warning(f"[Flags] Hit-record re-read failed: {exc}")
            return False
        if not rows:
            # Not a race -- the insert itself failed (already logged).
            return False

    row = max(rows, key=lambda r: r.get("created_at") or "")
    try:
        previous = json.loads(row.get("detail_json") or "{}")
        hits = int(previous.get("hits", 1)) + 1
    except Exception:
        hits = 2
    changes: dict = {
        "detail_json": json.dumps(
            {**detail, "hits": hits, "last_seen": now}, default=str
        )
    }
    if hits >= max(escalate_after, 2) and _rank(row.get("severity") or "warning") < _rank("error"):
        changes["severity"] = "error"
        logger.warning(
            "[Flags] Escalating %s/%s to error after %d unresolved hits",
            flag_type, subject_id, hits,
        )
    try:
        store.update("integrity_flags", row["id"], changes)
    except Exception as exc:
        logger.error(f"[Flags] Failed to update {flag_type} flag hit count: {exc}")
    return False
