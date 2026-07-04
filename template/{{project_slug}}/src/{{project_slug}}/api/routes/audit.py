"""
Deliberation audit API -- inspect metadata-only deliberation timelines.

  GET /api/v1/audit/deliberations/{correlation_id} -- Timeline of audit
      events for one deliberation run (404 if none recorded)

Events are produced by DeliberationAuditor (see
orchestration/deliberation_audit.py) and contain structural metadata
only -- phases, agent counts, durations, outcomes -- never prompt or
response content.

Security:
  - Auth required (same bearer scheme as the other routes)
  - Results are tenant-scoped via the AuthContext
  - Requires app.state.deliberation_auditor (503 when not configured)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ..middleware.auth import AuthContext, verify_api_key

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/audit/deliberations/{correlation_id}")
async def get_deliberation_timeline(
    correlation_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> dict:
    """Return the audit timeline for one deliberation run."""
    auditor = getattr(request.app.state, "deliberation_auditor", None)
    if auditor is None:
        raise HTTPException(
            status_code=503,
            detail="Deliberation audit not available (auditor not configured)",
        )

    events = [
        e
        for e in auditor.get_timeline(correlation_id)
        if e.get("tenant_id") == auth.tenant_id
    ]
    if not events:
        raise HTTPException(
            status_code=404,
            detail=f"No audit events for deliberation '{correlation_id}'",
        )
    return {
        "correlation_id": correlation_id,
        "events": events,
        "total": len(events),
    }
