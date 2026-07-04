"""
Correction erasure -- GDPR Article 17 hard-delete ("right to be forgotten").

Learned corrections can contain user-supplied text, so a data-subject
erasure request must be able to remove them permanently. This module
deletes the corrections row itself (not a soft-delete or a status flip)
and records a metadata-only audit event afterwards.

Safety rails:
  - Daily cap per tenant (ERASURE_DAILY_CAP env, default 10) so a
    compromised credential cannot silently bulk-wipe learned knowledge.
    The cap is counted from erasure audit events, so it survives restarts.
  - Tenant-scoped: an erasure request can only remove corrections that
    belong to the caller's tenant.
  - The audit event stores only the correction id and actor -- never the
    correction text (which is gone).

What this is NOT: a full compliance program. Retention policy, legal
hold, and backups that may still contain the data are deployment
concerns (see PLATFORM_GUIDE.md compliance section).
"""

import json
import logging
import os
import uuid
from datetime import datetime, timedelta

from .store import LearningStore

logger = logging.getLogger(__name__)

DEFAULT_ERASURE_DAILY_CAP = 10
ERASURE_EVENT_TYPE = "correction_erasure"


class ErasureCapExceededError(Exception):
    """Raised when a tenant's daily erasure cap is reached."""

    def __init__(self, current_count: int, cap: int):
        self.current_count = current_count
        self.cap = cap
        super().__init__(
            f"Erasure daily cap reached ({current_count}/{cap}); "
            "retry after 24h or raise ERASURE_DAILY_CAP"
        )


def _daily_cap() -> int:
    raw = os.environ.get("ERASURE_DAILY_CAP", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_ERASURE_DAILY_CAP


def _erasures_today(store: LearningStore, tenant_id: str) -> int:
    """Count erasure audit events for this tenant in the last 24 hours.

    The store protocol only supports equality filters, so the time window
    is applied in Python. Erasure volumes are capped at ~tens/day, so the
    row count here stays trivially small.
    """
    rows = store.query(
        "audit_events",
        {"event_type": ERASURE_EVENT_TYPE, "tenant_id": tenant_id},
        order_by="created_at DESC",
        limit=200,
    )
    cutoff = datetime.now() - timedelta(hours=24)
    count = 0
    for row in rows:
        try:
            created = datetime.fromisoformat(row.get("created_at", ""))
        except ValueError:
            continue
        if created >= cutoff:
            count += 1
    return count


def erase_correction(
    store: LearningStore,
    correction_id: str,
    tenant_id: str = "default",
    actor: str = "",
) -> dict:
    """
    Permanently delete a correction (GDPR Art. 17).

    Returns a summary dict: {"correction_id", "erased", "erasures_today"}.

    Raises ValueError if the correction does not exist in this tenant
    (missing and cross-tenant look identical -- no existence leak).
    Raises ErasureCapExceededError when the tenant's daily cap is reached.
    """
    cap = _daily_cap()
    used = _erasures_today(store, tenant_id)
    if used >= cap:
        raise ErasureCapExceededError(used, cap)

    rows = store.query(
        "corrections", {"id": correction_id, "tenant_id": tenant_id}, limit=1
    )
    if not rows:
        raise ValueError(f"Correction {correction_id} not found")

    if not store.delete("corrections", correction_id):
        raise ValueError(f"Correction {correction_id} not found")

    logger.info(
        f"[Erasure] Correction {correction_id} erased (tenant '{tenant_id}')"
    )
    _record_erasure_event(store, correction_id, tenant_id, actor)
    return {
        "correction_id": correction_id,
        "erased": True,
        "erasures_today": used + 1,
    }


def _record_erasure_event(
    store: LearningStore, correction_id: str, tenant_id: str, actor: str
) -> None:
    """Best-effort audit event. Metadata only -- the erased text is gone.

    Failure here is logged, not raised: the deletion already happened and
    must not be reported as failed. Worst case the cap under-counts.
    """
    try:
        store.insert(
            "audit_events",
            {
                "id": str(uuid.uuid4())[:12],
                "correlation_id": f"erasure-{correction_id}",
                "event_type": ERASURE_EVENT_TYPE,
                "tenant_id": tenant_id,
                "outcome": "erased",
                "detail_json": json.dumps({"actor": actor[:64]}),
                "created_at": datetime.now().isoformat(),
            },
        )
    except Exception as exc:
        logger.warning(f"[Erasure] Audit event write failed: {exc}")
