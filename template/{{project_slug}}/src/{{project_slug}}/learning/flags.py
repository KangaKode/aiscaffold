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

Leaf module: imports stdlib only; the store is passed in.

Keep this file under 100 lines.
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
