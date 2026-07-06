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

Derived artifacts (measured coverage, honest scope):
  - Error schemas (error_schemas) are DERIVED from approved corrections:
    their description/mitigation text is distilled from correction
    reason/corrected_claim fields and each row records its
    source_correction_ids. Erasing a correction deletes every schema
    that cites it, then re-runs extraction so clusters that still
    qualify are rebuilt from the REMAINING corrections only -- the
    erased text does not survive in generalized form. The rebuilt
    count covers only the clusters that lost a schema -- untouched
    clusters are not reported as rebuilt.
  - Check-ins: proposing a correction opens an approval check-in whose
    prompt embeds the original/corrected claim verbatim, and check-in
    expiry only flips status (never deletes). When a CheckInManager is
    passed (the corrections API route passes the same manager that
    created them), erasure hard-deletes every check-in citing the
    erased correction, whatever its status. Library callers that skip
    the checkin_manager argument leave those rows in place -- the
    response's checkins_deleted count says what actually happened.
  - Reflections derive from round-table results, and the RAG indexes
    cover preferences and transcripts; none of them are derived from
    corrections, so correction erasure does not touch them (and has
    nothing to re-derive there).
  - Prompt context rebuilds live from the corrections table on every
    request (corrections.py / knowledge_context.py), so an erased row
    disappears from future prompts without extra work. Text already
    inside past LLM requests or provider-side logs is out of scope.

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
    checkin_manager=None,
) -> dict:
    """
    Permanently delete a correction (GDPR Art. 17), including derived
    error schemas that cite it and -- when a checkin_manager is passed --
    the approval check-ins that embed its text verbatim (see module
    docstring for the measured derived-artifact scope).

    Returns a summary dict: {"correction_id", "erased", "erasures_today",
    "derived_schemas_deleted", "derived_schemas_rebuilt",
    "checkins_deleted"}. derived_schemas_rebuilt counts only schemas
    re-created for the clusters that lost one.

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
    schemas_deleted, schemas_rebuilt = _erase_derived_schemas(
        store, correction_id, tenant_id
    )
    checkins_deleted = _erase_correction_checkins(checkin_manager, correction_id)
    _record_erasure_event(store, correction_id, tenant_id, actor)
    return {
        "correction_id": correction_id,
        "erased": True,
        "erasures_today": used + 1,
        "derived_schemas_deleted": schemas_deleted,
        "derived_schemas_rebuilt": schemas_rebuilt,
        "checkins_deleted": checkins_deleted,
    }


def _erase_derived_schemas(
    store: LearningStore, correction_id: str, tenant_id: str
) -> tuple[int, int]:
    """Delete error schemas citing the erased correction, then re-extract.

    Deleting first (rather than updating in place) guarantees the
    distilled text built from the erased correction is gone even when
    the remaining cluster no longer meets the extraction safeguards.
    The rebuilt count covers only the (agent_id, evidence_level)
    clusters that lost a schema here -- re-extraction touches the whole
    tenant, but schemas for unrelated clusters are not "rebuilt".
    Best-effort: a failure here is logged, never raised -- the source
    correction is already deleted, which must not be reported as failed.
    """
    deleted = 0
    deleted_clusters: set[tuple[str, str]] = set()
    try:
        for schema in store.query("error_schemas", {"tenant_id": tenant_id}):
            try:
                source_ids = json.loads(schema.get("source_correction_ids_json") or "[]")
            except json.JSONDecodeError:
                source_ids = []
            if correction_id in source_ids:
                if store.delete("error_schemas", schema.get("id", "")):
                    deleted += 1
                    deleted_clusters.add((
                        schema.get("agent_id") or "",
                        schema.get("evidence_level") or "",
                    ))
    except Exception as exc:
        logger.warning(f"[Erasure] Derived-schema cleanup failed: {exc}")
        return deleted, 0

    rebuilt = 0
    if deleted:
        logger.info(
            f"[Erasure] Deleted {deleted} error schema(s) citing "
            f"correction {correction_id}"
        )
        try:
            from .error_schemata import extract_error_schemas

            rebuilt = sum(
                1
                for s in extract_error_schemas(store, tenant_id=tenant_id)
                if (s.agent_id or "", s.evidence_level or "") in deleted_clusters
            )
        except Exception as exc:
            logger.warning(f"[Erasure] Schema re-extraction failed: {exc}")
    return deleted, rebuilt


def _erase_correction_checkins(checkin_manager, correction_id: str) -> int:
    """Best-effort: check-in prompts embed the correction text verbatim.

    Failure is logged, never raised -- the source correction is already
    gone; a 0 count in the response tells the caller the check-in sweep
    did not happen.
    """
    if checkin_manager is None:
        return 0
    try:
        return checkin_manager.erase_for_correction(correction_id)
    except Exception as exc:
        logger.warning(f"[Erasure] Check-in cleanup failed: {exc}")
        return 0


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
