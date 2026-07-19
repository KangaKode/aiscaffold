"""
Procedures API -- sibling lifecycle for type=procedure knowledge rows.

Endpoints mirror /corrections: POST/GET /procedures, per-id approve,
reject, retire, revalidate, supersede, DELETE. Claim corrections stay on
/corrections. Approved procedures are excluded from default grounding
(R1); see GOVERNANCE Non-Claim.

List wires the shared extraction guard (same cap as GET /corrections).

Keep this file under 480 lines.
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...learning.corrections import (
    STATUS_APPROVED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    STATUS_RETIRED,
    Correction,
    CorrectionsManager,
)
from ...learning.erasure import (
    ErasureBlockedBySuccessorError,
    ErasureCapExceededError,
    erase_correction,
)
from ...learning.extraction_guard import (
    MODE_CAPPED,
    enforcement_enabled,
    evaluate_extraction_mode,
    retry_after_seconds,
)
from ...learning.procedure_screen import ProcedureScreenError, screen as screen_procedure
from ...learning.supersession import (
    MissingUpdateIfError,
    SupersessionConflictError,
)
from ...observability.metrics import record_correction_lifecycle
from ...security import ValidationError, validate_identifier, validate_in_choices
from ..middleware.auth import AuthContext, verify_api_key

logger = logging.getLogger(__name__)
router = APIRouter()

PROCEDURE_TYPE = "procedure"
MAX_CLAIM_CHARS = 4000
MAX_REASON_CHARS = 2000
VALID_STATUSES = [STATUS_PROPOSED, STATUS_APPROVED, STATUS_REJECTED, STATUS_RETIRED]
_ID_RE = re.compile(r"^[a-zA-Z0-9-]{1,64}$")


class ProcedureProposal(BaseModel):
    """Request body for proposing a procedure."""

    agent_id: str = Field(..., min_length=1, max_length=100)
    original_claim: str = Field(..., min_length=1, max_length=MAX_CLAIM_CHARS)
    corrected_claim: str = Field(..., min_length=1, max_length=MAX_CLAIM_CHARS)
    reason: str = Field("", max_length=MAX_REASON_CHARS)
    evidence_level: str = Field("", max_length=50)
    session_id: str = Field("", max_length=100)


class ProcedureResponse(BaseModel):
    """A procedure as returned by the API."""

    id: str
    agent_id: str
    original_claim: str
    corrected_claim: str
    reason: str
    evidence_level: str
    status: str
    created_by: str
    approved_by: str
    last_validated_at: str
    last_validated_by: str
    valid_at: str = ""
    invalid_at: str = ""
    supersedes_id: str = ""
    type: str = PROCEDURE_TYPE
    created_at: str
    updated_at: str
    override_flags: list[str] = []


class ErasureResponse(BaseModel):
    """GDPR erasure result + derived-artifact cleanup counts."""

    correction_id: str
    erased: bool
    erasures_today: int
    derived_schemas_deleted: int = 0
    derived_schemas_rebuilt: int = 0
    checkins_deleted: int = 0


def _get_manager(request: Request) -> CorrectionsManager:
    manager = getattr(request.app.state, "corrections_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Procedures are not enabled: no corrections manager is "
                "configured (app.state.corrections_manager)"
            ),
        )
    return manager


def _to_response(
    correction: Correction, override_flags: list[str] | None = None
) -> ProcedureResponse:
    return ProcedureResponse(
        id=correction.id,
        agent_id=correction.agent_id,
        original_claim=correction.original_claim,
        corrected_claim=correction.corrected_claim,
        reason=correction.reason,
        evidence_level=correction.evidence_level,
        status=correction.status,
        created_by=correction.created_by,
        approved_by=correction.approved_by,
        last_validated_at=correction.last_validated_at,
        last_validated_by=correction.last_validated_by,
        valid_at=correction.valid_at,
        invalid_at=correction.invalid_at,
        supersedes_id=correction.supersedes_id,
        type=correction.type or PROCEDURE_TYPE,
        created_at=correction.created_at,
        updated_at=correction.updated_at,
        override_flags=override_flags or [],
    )


def _validated_id(procedure_id: str) -> str:
    if not _ID_RE.match(procedure_id):
        raise HTTPException(status_code=404, detail="Procedure not found")
    return procedure_id


async def _get_tenant_procedure(
    manager: CorrectionsManager, procedure_id: str, tenant_id: str
) -> Correction:
    """404 for missing, cross-tenant, or non-procedure rows."""
    row = await asyncio.to_thread(manager.get, _validated_id(procedure_id))
    if (
        row is None
        or row.tenant_id != tenant_id
        or (row.type or "") != PROCEDURE_TYPE
    ):
        raise HTTPException(status_code=404, detail="Procedure not found")
    return row


async def _run_override_screen(
    request: Request, correction: Correction, tenant_id: str
) -> list[str]:
    detector = getattr(request.app.state, "override_detector", None)
    if detector is None:
        return []
    try:
        return await asyncio.to_thread(
            detector.screen_and_flag, correction, tenant_id=tenant_id
        )
    except Exception as e:
        logger.warning(f"[ProceduresAPI] Override screening failed: {e}")
        return []


@router.post("/procedures", response_model=ProcedureResponse, status_code=201)
async def propose_procedure(
    proposal: ProcedureProposal,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> ProcedureResponse:
    """Propose a procedure. Hard-refuses on procedure_screen deny."""
    manager = _get_manager(request)
    try:
        validate_identifier(proposal.agent_id, "agent_id")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        screen_procedure(
            proposal.original_claim, proposal.corrected_claim, proposal.reason
        )
    except ProcedureScreenError as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        correction = await asyncio.to_thread(
            manager.propose,
            agent_id=proposal.agent_id,
            original_claim=proposal.original_claim,
            corrected_claim=proposal.corrected_claim,
            reason=proposal.reason,
            evidence_level=proposal.evidence_level,
            tenant_id=auth.tenant_id,
            session_id=proposal.session_id,
            created_by=auth.user_id,
            source_surface="api",
            type=PROCEDURE_TYPE,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    record_correction_lifecycle("propose")
    override_flags = await _run_override_screen(request, correction, auth.tenant_id)
    return _to_response(correction, override_flags)


@router.post(
    "/procedures/{procedure_id}/supersede",
    response_model=ProcedureResponse,
    status_code=201,
)
async def supersede_procedure(
    procedure_id: str,
    proposal: ProcedureProposal,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> ProcedureResponse:
    """Propose a successor procedure (same-type supersession)."""
    manager = _get_manager(request)
    ancestor = await _get_tenant_procedure(manager, procedure_id, auth.tenant_id)
    try:
        validate_identifier(proposal.agent_id, "agent_id")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        screen_procedure(
            proposal.original_claim, proposal.corrected_claim, proposal.reason
        )
    except ProcedureScreenError as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        successor = await asyncio.to_thread(
            manager.propose_supersession,
            ancestor.id,
            agent_id=proposal.agent_id,
            original_claim=proposal.original_claim,
            corrected_claim=proposal.corrected_claim,
            reason=proposal.reason,
            evidence_level=proposal.evidence_level,
            tenant_id=auth.tenant_id,
            session_id=proposal.session_id,
            created_by=auth.user_id,
            source_surface="api",
            type=PROCEDURE_TYPE,
        )
    except ValueError as e:
        message = str(e)
        if "not currently-valid" in message or "not found" in message:
            raise HTTPException(status_code=409, detail=message)
        if "same knowledge type" in message:
            raise HTTPException(status_code=409, detail=message)
        raise HTTPException(status_code=422, detail=message)
    record_correction_lifecycle("propose_supersession")
    override_flags = await _run_override_screen(request, successor, auth.tenant_id)
    return _to_response(successor, override_flags)


@router.get("/procedures", response_model=list[ProcedureResponse])
async def list_procedures(
    request: Request,
    status: str = "",
    agent_id: str = "",
    limit: int = 100,
    auth: AuthContext = Depends(verify_api_key),
) -> list[ProcedureResponse]:
    """List this tenant's procedures. Shares the extraction-guard cap."""
    manager = _get_manager(request)
    guard_mode = ""
    try:
        guard = await asyncio.to_thread(
            evaluate_extraction_mode,
            getattr(request.app.state, "learning_store", None),
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
            include_current=True,
        )
        guard_mode = guard["mode"]
    except Exception as e:
        logger.warning(f"[ProceduresAPI] Extraction guard failed (non-fatal): {e}")
    if guard_mode == MODE_CAPPED and enforcement_enabled():
        raise HTTPException(
            status_code=429,
            detail=(
                "Knowledge-read volume is capped for this window "
                "(extraction guard). Retry later."
            ),
            headers={"Retry-After": str(retry_after_seconds())},
        )
    try:
        if status:
            validate_in_choices(status, VALID_STATUSES, "status")
        if agent_id:
            validate_identifier(agent_id, "agent_id")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    page_limit = max(1, min(limit, 500))
    rows = await asyncio.to_thread(
        manager.list,
        tenant_id=auth.tenant_id,
        status=status,
        agent_id=agent_id,
        limit=page_limit,
        type=PROCEDURE_TYPE,
    )
    return [_to_response(c) for c in rows]


@router.post(
    "/procedures/{procedure_id}/approve", response_model=ProcedureResponse
)
async def approve_procedure(
    procedure_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> ProcedureResponse:
    """Approve a proposed procedure (four-eyes same as corrections)."""
    manager = _get_manager(request)
    await _get_tenant_procedure(manager, procedure_id, auth.tenant_id)
    try:
        approved = await asyncio.to_thread(
            manager.approve, procedure_id, approved_by=auth.user_id
        )
    except MissingUpdateIfError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except SupersessionConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    record_correction_lifecycle("approve")
    return _to_response(approved)


@router.post(
    "/procedures/{procedure_id}/reject", response_model=ProcedureResponse
)
async def reject_procedure(
    procedure_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> ProcedureResponse:
    """Reject a proposed procedure (terminal)."""
    manager = _get_manager(request)
    await _get_tenant_procedure(manager, procedure_id, auth.tenant_id)
    try:
        rejected = await asyncio.to_thread(
            manager.reject, procedure_id, rejected_by=auth.user_id
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    record_correction_lifecycle("reject")
    return _to_response(rejected)


@router.post(
    "/procedures/{procedure_id}/retire", response_model=ProcedureResponse
)
async def retire_procedure(
    procedure_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> ProcedureResponse:
    """Retire an approved procedure."""
    manager = _get_manager(request)
    await _get_tenant_procedure(manager, procedure_id, auth.tenant_id)
    try:
        retired = await asyncio.to_thread(manager.retire, procedure_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    record_correction_lifecycle("retire")
    return _to_response(retired)


@router.post(
    "/procedures/{procedure_id}/revalidate", response_model=ProcedureResponse
)
async def revalidate_procedure(
    procedure_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> ProcedureResponse:
    """Re-confirm a currently-valid approved procedure."""
    manager = _get_manager(request)
    await _get_tenant_procedure(manager, procedure_id, auth.tenant_id)
    try:
        revalidated = await asyncio.to_thread(
            manager.revalidate, procedure_id, validated_by=auth.user_id
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    record_correction_lifecycle("revalidate")
    return _to_response(revalidated)


@router.delete("/procedures/{procedure_id}", response_model=ErasureResponse)
async def erase_procedure(
    procedure_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> ErasureResponse:
    """GDPR Article 17 hard-delete for a procedure row."""
    manager = _get_manager(request)
    await _get_tenant_procedure(manager, procedure_id, auth.tenant_id)
    try:
        result = await asyncio.to_thread(
            erase_correction,
            manager.store,
            _validated_id(procedure_id),
            tenant_id=auth.tenant_id,
            actor=auth.user_id,
            checkin_manager=manager.checkin_manager,
        )
    except ErasureCapExceededError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except ErasureBlockedBySuccessorError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(e),
                "correction_id": e.correction_id,
                "successor_ids": e.successor_ids,
            },
        )
    except RuntimeError as e:
        logger.warning(f"[ProceduresAPI] Successor check failed: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError:
        raise HTTPException(status_code=404, detail="Procedure not found")
    return ErasureResponse(**result)
