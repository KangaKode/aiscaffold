"""
Check-ins API -- permission-based adaptation gates.

  GET  /api/v1/checkins              -- List pending check-ins
  POST /api/v1/checkins/{id}/respond -- Respond to a check-in (approve/reject)
  POST /api/v1/checkins/{id}/skip    -- Skip a check-in

Security:
  - Auth required for responding
  - Response content sanitized
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...learning.checkin_manager import CheckInManager
from ...security import ValidationError, validate_length
from ..middleware.auth import verify_api_key

logger = logging.getLogger(__name__)
router = APIRouter()


class CheckInResponse(BaseModel):
    """Check-in details returned to the client."""

    id: str
    checkin_type: str
    prompt: str
    suggested_action: str
    status: str
    created_at: str
    expires_at: str


class RespondRequest(BaseModel):
    """Request to respond to a pending check-in."""

    approved: bool = Field(..., description="True to approve, False to reject")
    response: str = Field("", description="Optional user comment")


def _get_checkin_mgr(request: Request) -> CheckInManager:
    mgr = getattr(request.app.state, "checkin_manager", None)
    if mgr is None:
        mgr = CheckInManager()
        request.app.state.checkin_manager = mgr
    return mgr


@router.get("/checkins")
async def list_pending_checkins(request: Request) -> dict:
    """List all pending check-ins awaiting user response."""
    mgr = _get_checkin_mgr(request)
    pending = mgr.get_pending()
    return {
        "checkins": [
            CheckInResponse(
                id=c.id,
                checkin_type=c.checkin_type,
                prompt=c.prompt,
                suggested_action=c.suggested_action,
                status=c.status,
                created_at=c.created_at,
                expires_at=c.expires_at,
            ).model_dump()
            for c in pending
        ],
        "total": len(pending),
    }


@router.post("/checkins/{checkin_id}/respond")
async def respond_to_checkin(
    checkin_id: str,
    respond_req: RespondRequest,
    request: Request,
    _key: str | None = Depends(verify_api_key),
) -> dict:
    """Respond to a pending check-in (approve or reject)."""
    try:
        validate_length(checkin_id, "checkin_id", min_length=1, max_length=50)
        if respond_req.response:
            validate_length(respond_req.response, "response", max_length=5000)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    mgr = _get_checkin_mgr(request)
    result = mgr.respond(
        checkin_id=checkin_id,
        approved=respond_req.approved,
        response=respond_req.response,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Check-in '{checkin_id}' not found or already resolved",
        )

    return {
        "status": result.status,
        "checkin_id": result.id,
        "resolved_at": result.resolved_at,
    }


@router.post("/checkins/{checkin_id}/skip")
async def skip_checkin(
    checkin_id: str,
    request: Request,
    _key: str | None = Depends(verify_api_key),
) -> dict:
    """Skip a check-in (decide later)."""
    mgr = _get_checkin_mgr(request)
    if not mgr.skip(checkin_id):
        raise HTTPException(
            status_code=404,
            detail=f"Check-in '{checkin_id}' not found or already resolved",
        )
    return {"status": "skipped", "checkin_id": checkin_id}
