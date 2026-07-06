"""
Governance report API -- one summary endpoint for periodic oversight.

  GET /api/v1/reports/governance?from_date=&to_date= -- Tenant-scoped
      counts of deliberation outcomes, integrity flags, corrections
      lifecycle + stale-knowledge summary, reflections, and budget
      spend for a date window.

The heavy lifting (section assembly, whitelist-only output, visibility
horizon disclosure, daily rate cap) lives in
learning/governance_report.py; this module is boundary validation and
HTTP mapping only.

Security:
  - Auth required; everything is scoped to auth.tenant_id.
  - Output is counts/ids/enums/timestamps only -- no free text, ever.
  - Per-user daily cap (REPORT_DAILY_CAP, default 10) -> 429; every
    generated report is recorded as a metadata-only audit event.
  - Deployments exposing reports beyond operators should gate this
    route with the RBAC recipe (docs/PLATFORM_GUIDE.md, Step 2).

Keep this file under 200 lines.
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from ...learning.governance_report import (
    ReportCapExceededError,
    build_governance_report,
    enforce_report_cap,
    record_report_event,
)
from ...security import ValidationError, validate_length
from ..middleware.auth import AuthContext, verify_api_key

logger = logging.getLogger(__name__)
router = APIRouter()

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 366
MIN_YEAR = 2000
MAX_YEAR = 2100


def _parse_date(raw: str, field_name: str) -> datetime:
    """Parse an ISO date/datetime query param; HTTP 422 on anything else."""
    try:
        validate_length(raw, field_name, min_length=4, max_length=32)
        parsed = datetime.fromisoformat(raw)
    except (ValidationError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be an ISO date (e.g. 2026-07-01)",
        )
    if not MIN_YEAR <= parsed.year <= MAX_YEAR:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} year must be between {MIN_YEAR} and {MAX_YEAR}",
        )
    return parsed


def _resolve_window(from_date: str, to_date: str) -> tuple[datetime, datetime]:
    """(from_dt, to_dt_exclusive) with defaults: last DEFAULT_WINDOW_DAYS.

    A date-only to_date means "through the end of that day", so the
    exclusive upper bound is midnight of the following day.
    """
    if to_date:
        to_dt = _parse_date(to_date, "to_date")
    else:
        to_dt = datetime.now()
    to_exclusive = to_dt + timedelta(days=1) if _is_date_only(to_date) else to_dt
    if from_date:
        from_dt = _parse_date(from_date, "from_date")
    else:
        from_dt = to_dt - timedelta(days=DEFAULT_WINDOW_DAYS)
    if from_dt >= to_exclusive:
        raise HTTPException(
            status_code=422, detail="from_date must be before to_date"
        )
    if to_exclusive - from_dt > timedelta(days=MAX_WINDOW_DAYS):
        raise HTTPException(
            status_code=422,
            detail=f"Date range too large (max {MAX_WINDOW_DAYS} days)",
        )
    return from_dt, to_exclusive


def _is_date_only(raw: str) -> bool:
    return bool(raw) and len(raw.strip()) == 10


def _get_store(request: Request):
    store = getattr(request.app.state, "learning_store", None)
    if store is None:
        manager = getattr(request.app.state, "corrections_manager", None)
        store = manager.store if manager is not None else None
    if store is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Governance reports are not available: no learning store is "
                "configured on this deployment"
            ),
        )
    return store


@router.get("/reports/governance")
async def get_governance_report(
    request: Request,
    from_date: str = "",
    to_date: str = "",
    auth: AuthContext = Depends(verify_api_key),
) -> dict:
    """Generate the tenant's governance report for a date window.

    Defaults to the last 30 days. Sections disclose their actual
    visibility horizon: each fetches the most recent rows up to a
    per-section cap, and when the requested window starts before the
    oldest fetched row the section is annotated coverage_partial=true
    (counts are then a floor, not exact history).
    """
    store = _get_store(request)
    from_dt, to_dt = _resolve_window(from_date, to_date)

    try:
        cap_used = enforce_report_cap(store, auth.tenant_id, auth.user_id)
    except ReportCapExceededError as e:
        raise HTTPException(status_code=429, detail=str(e))

    report = build_governance_report(store, auth.tenant_id, from_dt, to_dt)
    record_report_event(store, auth.tenant_id, auth.user_id)
    report["reports_today"] = cap_used + 1
    return report
