"""
Governance report -- tenant-scoped counts for periodic oversight review.

Aggregates what the governance layer already records (audit events,
integrity flags, corrections lifecycle, reflections, budget spend) into
one metadata-only summary for a date window. Consumed by
GET /api/v1/reports/governance (api/routes/reports.py).

Whitelist-only output, by construction: every section emits counts,
ids, enum values, and timestamps ONLY. detail_json, correction text,
reflection text, and every other free-text column are never read into
the report, so it is safe to retain and share broadly.

Visibility horizon: the store protocol supports equality filters plus
a limit, so each section fetches the tenant's most recent
SECTION_FETCH_CAP rows (newest first) and applies the date window in
Python. When a section's fetch hits the cap, older rows exist that the
report cannot see; the section then reports the actual covered range
(coverage_from = oldest fetched row) and coverage_partial=true if the
requested window starts before that horizon. Callers must treat a
partial section as "at least these counts", not exact history.

Rate cap: report generation is capped per user per day
(REPORT_DAILY_CAP env, default 10), counted from meta audit events the
same way erasure caps are (learning/erasure.py), so the cap survives
restarts. Every generated report leaves an audit event.

Keep this file under 350 lines.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timedelta

from .aging import row_is_stale, stale_days
from .erasure import ERASURE_EVENT_TYPE
from .store import LearningStore

logger = logging.getLogger(__name__)

DEFAULT_REPORT_DAILY_CAP = 10
REPORT_EVENT_TYPE = "governance_report"
# Per-section row-fetch cap (newest first). Large enough that a month of
# normal activity fits comfortably; small enough that one report cannot
# table-scan years of history.
SECTION_FETCH_CAP = 2000


class ReportCapExceededError(Exception):
    """Raised when a user's daily report-generation cap is reached."""

    def __init__(self, current_count: int, cap: int):
        self.current_count = current_count
        self.cap = cap
        super().__init__(
            f"Report daily cap reached ({current_count}/{cap}); "
            "retry after 24h or raise REPORT_DAILY_CAP"
        )


def _daily_cap() -> int:
    raw = os.environ.get("REPORT_DAILY_CAP", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_REPORT_DAILY_CAP


def reports_today(store: LearningStore, tenant_id: str, user_id: str) -> int:
    """Count this user's report audit events in the last 24 hours.

    The per-user scope rides on correlation_id (an equality-filterable
    column); the 24h window is applied in Python, mirroring
    erasure._erasures_today.
    """
    rows = store.query(
        "audit_events",
        {
            "event_type": REPORT_EVENT_TYPE,
            "tenant_id": tenant_id,
            "correlation_id": f"govreport-{user_id}"[:64],
        },
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


def enforce_report_cap(
    store: LearningStore, tenant_id: str, user_id: str
) -> int:
    """Raise ReportCapExceededError at the user's daily cap; otherwise
    return how many reports this user has generated in the last 24h."""
    cap = _daily_cap()
    used = reports_today(store, tenant_id, user_id)
    if used >= cap:
        raise ReportCapExceededError(used, cap)
    return used


def record_report_event(
    store: LearningStore, tenant_id: str, user_id: str
) -> None:
    """Best-effort audit event for one generated report (metadata only)."""
    try:
        store.insert(
            "audit_events",
            {
                "id": str(uuid.uuid4())[:12],
                "correlation_id": f"govreport-{user_id}"[:64],
                "event_type": REPORT_EVENT_TYPE,
                "tenant_id": tenant_id,
                "outcome": "generated",
                "detail_json": json.dumps({"actor": user_id[:64]}),
                "created_at": datetime.now().isoformat(),
            },
        )
    except Exception as exc:
        logger.warning(f"[GovernanceReport] Audit event write failed: {exc}")


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return None


def _fetch_section(
    store: LearningStore,
    table: str,
    tenant_id: str,
    from_dt: datetime,
    to_dt: datetime,
    time_column: str = "created_at",
) -> tuple[list[dict], dict]:
    """
    Most recent SECTION_FETCH_CAP rows for the tenant, window-filtered in
    Python. Returns (rows_in_window, coverage) where coverage discloses
    the actual visible range for this section (see module docstring).
    """
    fetched = store.query(
        table,
        {"tenant_id": tenant_id},
        order_by=f"{time_column} DESC",
        limit=SECTION_FETCH_CAP,
    )
    coverage = {
        "coverage_from": from_dt.isoformat(),
        "coverage_to": to_dt.isoformat(),
        "coverage_partial": False,
    }
    if len(fetched) >= SECTION_FETCH_CAP and fetched:
        oldest = fetched[-1].get(time_column, "")
        coverage["coverage_from"] = oldest
        oldest_dt = _parse(oldest)
        if oldest_dt is None or from_dt < oldest_dt:
            coverage["coverage_partial"] = True
    in_window = []
    for row in fetched:
        stamp = _parse(row.get(time_column, ""))
        if stamp is not None and from_dt <= stamp < to_dt:
            in_window.append(row)
    return in_window, coverage


def _count_by(rows: list[dict], column: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get(column) or "")
        counts[key] = counts.get(key, 0) + 1
    return counts


# Meta audit events the platform writes about itself (rate-cap ledgers,
# not deliberation activity). Excluded from the deliberation section so
# generating reports or processing erasures cannot inflate its counts.
META_EVENT_TYPES = {REPORT_EVENT_TYPE, ERASURE_EVENT_TYPE}


def _deliberation_section(rows: list[dict], coverage: dict) -> dict:
    rows = [r for r in rows if r.get("event_type") not in META_EVENT_TYPES]
    completed = [r for r in rows if r.get("event_type") == "deliberation_completed"]
    return {
        "events_by_type": _count_by(rows, "event_type"),
        "deliberation_outcomes": _count_by(completed, "outcome"),
        "total_events": len(rows),
        **coverage,
    }


def _flags_section(rows: list[dict], coverage: dict) -> dict:
    return {
        "by_type": _count_by(rows, "flag_type"),
        "by_severity": _count_by(rows, "severity"),
        "resolved": sum(1 for r in rows if r.get("resolved")),
        "unresolved": sum(1 for r in rows if not r.get("resolved")),
        **coverage,
    }


def _corrections_section(
    windowed: list[dict],
    all_fetched: list[dict],
    coverage: dict,
    from_dt: datetime,
    to_dt: datetime,
) -> dict:
    """Lifecycle-activity counts for the window, plus a point-in-time
    staleness snapshot over every fetched approved row (staleness is a
    property of NOW, not of the reporting window).

    Windowing is by updated_at -- every lifecycle transition (approve /
    reject / retire) bumps it -- so approving an old proposal inside the
    window counts, and old corrections without window activity do not.
    Revalidations deliberately do NOT bump updated_at; they are counted
    separately via last_validated_at.
    """
    revalidations_in_window = 0
    for row in all_fetched:
        stamp = _parse(row.get("last_validated_at") or "")
        if stamp is not None and from_dt <= stamp < to_dt:
            revalidations_in_window += 1
    approved = [r for r in all_fetched if r.get("status") == "approved"]
    stale_now = [r for r in approved if row_is_stale(r)]
    # Fresh purely because someone revalidated: would be stale on the
    # updated_at fallback alone.
    revalidation_carried = [
        r
        for r in approved
        if r.get("last_validated_at")
        and not row_is_stale(r)
        and row_is_stale({**r, "last_validated_at": ""})
    ]
    self_revalidated_ids = sorted(
        str(r.get("id") or "")
        for r in approved
        if r.get("last_validated_by")
        and r.get("last_validated_by") == r.get("created_by")
    )
    return {
        # Corrections with lifecycle activity (updated_at) in the window,
        # bucketed by their CURRENT status.
        "lifecycle_activity_by_status": _count_by(windowed, "status"),
        "revalidations_in_window": revalidations_in_window,
        "stale_days_threshold": stale_days(),
        "stale_approved_now": len(stale_now),
        "fresh_only_via_revalidation": len(revalidation_carried),
        "self_revalidated_ids": self_revalidated_ids,
        **coverage,
    }


def build_governance_report(
    store: LearningStore,
    tenant_id: str,
    from_dt: datetime,
    to_dt: datetime,
) -> dict:
    """
    Assemble the full report for [from_dt, to_dt). Counts, ids, enums,
    and timestamps only -- see the module docstring for the whitelist
    and visibility-horizon contracts.
    """
    audit_rows, audit_cov = _fetch_section(
        store, "audit_events", tenant_id, from_dt, to_dt
    )
    flag_rows, flag_cov = _fetch_section(
        store, "integrity_flags", tenant_id, from_dt, to_dt
    )
    # Window corrections by updated_at so lifecycle transitions inside
    # the window on OLDER corrections are counted (see _corrections_section).
    corr_rows, corr_cov = _fetch_section(
        store, "corrections", tenant_id, from_dt, to_dt,
        time_column="updated_at",
    )
    # Staleness snapshot needs every fetched approved row, not only the
    # window slice; refetch cheaply at the same cap.
    corr_all = store.query(
        "corrections",
        {"tenant_id": tenant_id},
        order_by="created_at DESC",
        limit=SECTION_FETCH_CAP,
    )
    refl_rows, refl_cov = _fetch_section(
        store, "reflections", tenant_id, from_dt, to_dt
    )
    spend_rows, spend_cov = _fetch_section(
        store, "budget_spend", tenant_id, from_dt, to_dt
    )

    return {
        "tenant_id": tenant_id,
        "from_date": from_dt.isoformat(),
        "to_date": to_dt.isoformat(),
        "generated_at": datetime.now().isoformat(),
        "sections": {
            "deliberation": _deliberation_section(audit_rows, audit_cov),
            "integrity_flags": _flags_section(flag_rows, flag_cov),
            "corrections": _corrections_section(
                corr_rows, corr_all, corr_cov, from_dt, to_dt
            ),
            "reflections": {
                "count": len(refl_rows),
                "by_type": _count_by(refl_rows, "reflection_type"),
                **refl_cov,
            },
            "budget": {
                "entries": len(spend_rows),
                "total_usd": round(
                    sum(float(r.get("amount_usd") or 0) for r in spend_rows), 6
                ),
                "by_model": _count_by(spend_rows, "model"),
                **spend_cov,
            },
        },
    }
