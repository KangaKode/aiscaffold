"""
Graduation API -- promote stable preferences to the cross-project global profile.

  GET  /api/v1/graduation/candidates -- Preferences stable enough to graduate
  POST /api/v1/graduation/propose    -- Open a check-in per candidate
  POST /api/v1/graduation/apply      -- Apply a candidate whose check-in was approved

Graduation is check-in gated end to end: /propose only creates pending
check-ins, and /apply refuses anything whose check-in is not an APPROVED
graduation check-in. Nothing reaches the global profile without an
explicit human approval recorded first.

The graduation engine reads the legacy learning tables, which are keyed
by project_id; the caller's tenant_id maps onto it.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...learning.checkin_manager import CheckInManager, CheckInStatus
from ...learning.graduation import GraduationCandidate, GraduationEngine
from ...security import ValidationError, validate_length
from ..middleware.auth import AuthContext, verify_api_key
from ..middleware.rate_limit import check_rate_limit

logger = logging.getLogger(__name__)
router = APIRouter()


class CandidateResponse(BaseModel):
    """A preference eligible for promotion to the global profile."""

    key: str
    value: str
    source_project: str
    rule_name: str
    confidence: float
    evidence: str


class ProposalResponse(BaseModel):
    """A check-in opened for one graduation candidate."""

    checkin_id: str
    key: str
    value: str


class ApplyRequest(BaseModel):
    """Apply a graduation whose check-in was approved."""

    checkin_id: str = Field(..., description="ID of an approved graduation check-in")


class ApplyResponse(BaseModel):
    """Result of applying a graduation."""

    checkin_id: str
    key: str
    value: str
    applied: bool


def _engine(request: Request, auth: AuthContext) -> GraduationEngine:
    mgr = getattr(request.app.state, "checkin_manager", None) or CheckInManager()
    return GraduationEngine(project_id=auth.tenant_id, checkin_manager=mgr)


@router.get("/graduation/candidates", response_model=list[CandidateResponse])
async def list_candidates(
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> list[CandidateResponse]:
    """List preferences stable enough to be promoted to the global profile."""
    candidates = _engine(request, auth).find_all_candidates()
    return [
        CandidateResponse(
            key=c.key,
            value=c.value,
            source_project=c.source_project,
            rule_name=c.rule_name,
            confidence=c.confidence,
            evidence=c.evidence,
        )
        for c in candidates
    ]


@router.post("/graduation/propose", response_model=list[ProposalResponse])
async def propose_graduations(
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> list[ProposalResponse]:
    """Open one pending check-in per current candidate. Nothing is applied."""
    engine = _engine(request, auth)
    proposals = []
    for candidate in engine.find_all_candidates():
        checkin_id = engine.propose_graduation(candidate)
        proposals.append(
            ProposalResponse(checkin_id=checkin_id, key=candidate.key, value=candidate.value)
        )
    return proposals


@router.post("/graduation/apply", response_model=ApplyResponse)
async def apply_graduation(
    body: ApplyRequest,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> ApplyResponse:
    """Apply a graduation to the global profile. The check-in must be an
    APPROVED graduation check-in belonging to the caller's tenant."""
    try:
        validate_length(body.checkin_id, "checkin_id", min_length=1, max_length=128)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    mgr = getattr(request.app.state, "checkin_manager", None) or CheckInManager()
    checkin = mgr.get(body.checkin_id)
    if (
        checkin is None
        or checkin.checkin_type != "graduation"
        or checkin.project_id != auth.tenant_id
    ):
        raise HTTPException(status_code=404, detail="Graduation check-in not found")
    if checkin.status != CheckInStatus.APPROVED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Check-in is '{checkin.status}'; approve it via "
                "POST /api/v1/checkins/{id}/respond first"
            ),
        )

    ctx = checkin.context or {}
    key = str(ctx.get("candidate_key", ""))
    value = str(ctx.get("candidate_value", ""))
    if not key:
        raise HTTPException(
            status_code=409, detail="Check-in has no graduation candidate context"
        )

    candidate = GraduationCandidate(
        key=key,
        value=value,
        source_project=auth.tenant_id,
        rule_name=str(ctx.get("rule", "")),
        confidence=float(ctx.get("confidence", 0.5) or 0.5),
        evidence=str(ctx.get("evidence", "")),
    )
    _engine(request, auth).apply_graduation(candidate)
    return ApplyResponse(
        checkin_id=body.checkin_id, key=key, value=value, applied=True
    )
