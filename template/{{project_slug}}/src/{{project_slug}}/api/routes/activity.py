"""
Activity anomalies API -- review and resolve integrity flags.

  GET  /api/v1/activity/anomalies                 -- List unresolved flags
                                                     (optional ?flag_type=)
  POST /api/v1/activity/anomalies/{flag_id}/resolve -- Mark a flag resolved

Flags are produced by the analytics modules (override screening, collusion
detection, activity thresholds, agent behavior baselines). Nothing is
acted on automatically -- this API is how a human reviews and closes them.

Security:
  - Auth required (same bearer scheme as the other routes)
  - Results are tenant-scoped via the AuthContext
  - flag_type is validated as a safe identifier
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...learning.store import LearningStore, get_learning_store
from ...security import ValidationError, validate_identifier
from ..middleware.auth import AuthContext, verify_api_key

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_RESULTS = 200


def _get_store(request: Request) -> LearningStore:
    """Fetch the shared learning store (create lazily if the gateway didn't)."""
    store = getattr(request.app.state, "learning_store", None)
    if store is None:
        try:
            store = get_learning_store()
        except Exception as exc:
            logger.error(f"[ActivityAPI] Learning store unavailable: {exc}")
            raise HTTPException(
                status_code=503, detail="Learning store unavailable"
            )
        request.app.state.learning_store = store
    return store


@router.get("/activity/anomalies")
async def list_anomalies(
    request: Request,
    flag_type: str = Query("", description="Filter by flag type (optional)"),
    auth: AuthContext = Depends(verify_api_key),
) -> dict:
    """List unresolved integrity flags for the caller's tenant."""
    if flag_type:
        try:
            validate_identifier(flag_type, "flag_type")
        except ValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

    store = _get_store(request)
    filters = {"tenant_id": auth.tenant_id, "resolved": 0}
    if flag_type:
        filters["flag_type"] = flag_type
    rows = store.query(
        "integrity_flags",
        filters,
        order_by="created_at DESC",
        limit=MAX_RESULTS,
    )
    return {"anomalies": rows, "total": len(rows)}


@router.post("/activity/anomalies/{flag_id}/resolve")
async def resolve_anomaly(
    flag_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> dict:
    """Mark an integrity flag as resolved (human reviewed it)."""
    store = _get_store(request)
    rows = store.query(
        "integrity_flags",
        {"id": flag_id, "tenant_id": auth.tenant_id},
        limit=1,
    )
    if not rows:
        raise HTTPException(
            status_code=404, detail=f"Anomaly flag '{flag_id}' not found"
        )
    if rows[0].get("resolved"):
        return {"status": "already_resolved", "flag_id": flag_id}

    store.update("integrity_flags", flag_id, {"resolved": 1})
    logger.info(f"[ActivityAPI] Resolved flag {flag_id} (tenant={auth.tenant_id})")
    return {"status": "resolved", "flag_id": flag_id}
