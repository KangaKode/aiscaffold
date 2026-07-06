"""
Integrity-flag helpers shared by the detection modules.

The detection modules (timing_analysis, extraction_guard,
approval_patterns) all persist findings the same way: one
integrity_flags row per finding, deduplicated by a persistence-level
cooldown -- before inserting, the store is queried for an UNRESOLVED
flag of the same type and subject, and the insert is skipped when one
exists. A human resolving the flag (POST /activity/anomalies/{id}/resolve)
re-arms detection for that subject.

Leaf module: imports stdlib only; the store is passed in.

Keep this file under 100 lines.
"""

import json
import logging
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)


def has_unresolved_flag(
    store, flag_type: str, subject_id: str, tenant_id: str = "default"
) -> bool:
    """True when an unresolved flag of this type+subject already exists."""
    try:
        rows = store.query(
            "integrity_flags",
            {
                "flag_type": flag_type,
                "subject_id": subject_id,
                "tenant_id": tenant_id,
                "resolved": 0,
            },
            limit=1,
        )
        return bool(rows)
    except Exception as exc:
        logger.warning(f"[Flags] Cooldown query failed (treating as flagged): {exc}")
        # Fail toward "already flagged" so a broken store cannot cause
        # unbounded duplicate inserts.
        return True


def insert_flag_once(
    store,
    flag_type: str,
    subject_id: str,
    tenant_id: str,
    detail: dict,
    severity: str = "warning",
) -> bool:
    """
    Insert an integrity flag unless an unresolved one already exists for
    the same type+subject (persistence-level cooldown). Best-effort:
    storage errors are logged, never raised. Returns True if inserted.
    """
    if has_unresolved_flag(store, flag_type, subject_id, tenant_id):
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
