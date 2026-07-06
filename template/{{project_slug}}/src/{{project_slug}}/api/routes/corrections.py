"""
Corrections API -- the full correction lifecycle over HTTP.

  POST   /api/v1/corrections                    -- Propose a correction
  GET    /api/v1/corrections                    -- List (filter by status/agent,
                                                   ?stale=true for aging review)
  POST   /api/v1/corrections/{id}/approve       -- Approve (four-eyes)
  POST   /api/v1/corrections/{id}/reject        -- Reject (terminal)
  POST   /api/v1/corrections/{id}/retire        -- Retire an approved one
  POST   /api/v1/corrections/{id}/revalidate    -- Re-confirm an approved one
                                                   (refreshes staleness clock)
  DELETE /api/v1/corrections/{id}               -- GDPR Art. 17 hard-delete

API-first: this is the only write path a non-Python client needs to
participate in the learning loop. The manager is expected at
app.state.corrections_manager (wired at startup); 503 when absent so
callers can tell "learning not enabled" apart from "no corrections".

Security:
  - All endpoints require API key; tenant comes from AuthContext, never
    from the request body.
  - created_by / approved_by are the authenticated caller's user id, so
    the four-eyes rule cannot be spoofed by naming someone else.
  - Proposals are screened by the override detector (results returned to
    the caller and flagged for review) on top of the manager's built-in
    PII redaction and content-policy gate.
  - Status-transition violations map to 409, not 500.
"""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...learning.aging import is_stale
from ...learning.corrections import (
    STATUS_APPROVED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    STATUS_RETIRED,
    Correction,
    CorrectionsManager,
)
from ...learning.erasure import ErasureCapExceededError, erase_correction
from ...learning.extraction_guard import (
    MODE_CAPPED,
    enforcement_enabled,
    evaluate_extraction_mode,
    retry_after_seconds,
)
from ...observability.metrics import record_correction_lifecycle
from ...security import ValidationError, validate_identifier, validate_in_choices
from ..middleware.auth import AuthContext, verify_api_key

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_CLAIM_CHARS = 4000
MAX_REASON_CHARS = 2000

VALID_STATUSES = [STATUS_PROPOSED, STATUS_APPROVED, STATUS_REJECTED, STATUS_RETIRED]

# Correction ids are short uuid prefixes; anything else can 404 early
# without touching the store.
_CORRECTION_ID_RE = re.compile(r"^[a-zA-Z0-9-]{1,64}$")


class CorrectionProposal(BaseModel):
    """Request body for proposing a correction."""

    agent_id: str = Field(..., min_length=1, max_length=100)
    original_claim: str = Field(..., min_length=1, max_length=MAX_CLAIM_CHARS)
    corrected_claim: str = Field(..., min_length=1, max_length=MAX_CLAIM_CHARS)
    reason: str = Field("", max_length=MAX_REASON_CHARS)
    evidence_level: str = Field("", max_length=50)
    session_id: str = Field("", max_length=100)


class CorrectionResponse(BaseModel):
    """A correction as returned by the API."""

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
    created_at: str
    updated_at: str
    override_flags: list[str] = []


class ErasureResponse(BaseModel):
    """Result of a GDPR erasure."""

    correction_id: str
    erased: bool
    erasures_today: int


def _get_manager(request: Request) -> CorrectionsManager:
    manager = getattr(request.app.state, "corrections_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Corrections are not enabled: no corrections manager is "
                "configured on this deployment (app.state.corrections_manager)"
            ),
        )
    return manager


def _to_response(
    correction: Correction, override_flags: list[str] | None = None
) -> CorrectionResponse:
    return CorrectionResponse(
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
        created_at=correction.created_at,
        updated_at=correction.updated_at,
        override_flags=override_flags or [],
    )


def _validated_id(correction_id: str) -> str:
    if not _CORRECTION_ID_RE.match(correction_id):
        raise HTTPException(status_code=404, detail="Correction not found")
    return correction_id


def _get_tenant_correction(
    manager: CorrectionsManager, correction_id: str, tenant_id: str
) -> Correction:
    """404 for missing AND cross-tenant ids -- no existence leak."""
    correction = manager.get(_validated_id(correction_id))
    if correction is None or correction.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Correction not found")
    return correction


@router.post("/corrections", response_model=CorrectionResponse, status_code=201)
async def propose_correction(
    proposal: CorrectionProposal,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> CorrectionResponse:
    """Propose a correction. It only influences prompts after approval."""
    manager = _get_manager(request)
    try:
        validate_identifier(proposal.agent_id, "agent_id")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        correction = manager.propose(
            agent_id=proposal.agent_id,
            original_claim=proposal.original_claim,
            corrected_claim=proposal.corrected_claim,
            reason=proposal.reason,
            evidence_level=proposal.evidence_level,
            tenant_id=auth.tenant_id,
            session_id=proposal.session_id,
            created_by=auth.user_id,
        )
    except ValueError as e:
        # Content-policy rejection: refuse the write, tell the caller why.
        raise HTTPException(status_code=422, detail=str(e))
    record_correction_lifecycle("propose")

    override_flags: list[str] = []
    detector = getattr(request.app.state, "override_detector", None)
    if detector is not None:
        try:
            override_flags = detector.screen_and_flag(
                correction, tenant_id=auth.tenant_id
            )
        except Exception as e:
            logger.warning(f"[CorrectionsAPI] Override screening failed: {e}")

    return _to_response(correction, override_flags)


@router.get("/corrections", response_model=list[CorrectionResponse])
async def list_corrections(
    request: Request,
    status: str = "",
    agent_id: str = "",
    limit: int = 100,
    stale: bool = False,
    auth: AuthContext = Depends(verify_api_key),
) -> list[CorrectionResponse]:
    """List this tenant's corrections, newest first.

    stale=true restricts the listing to APPROVED corrections whose
    freshness timestamp (last_validated_at, falling back to updated_at
    for rows that predate revalidation) is older than
    CORRECTION_STALE_DAYS -- the knowledge-aging review queue. The
    staleness filter is applied in Python over the (limit-clamped)
    approved listing, so a page of results may contain fewer than
    `limit` entries.

    The extraction guard evaluates knowledge-read volume on every call
    (detection-only: elevated/capped write integrity flags). The ONLY
    enforcement is opt-in: with EXTRACTION_GUARD_ENFORCE=true a capped
    caller gets 429 + Retry-After. The listing is never silently
    truncated -- callers get everything or an explicit 429.
    """
    manager = _get_manager(request)
    guard_mode = ""
    try:
        guard = evaluate_extraction_mode(
            getattr(request.app.state, "learning_store", None),
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
            # The middleware records this request only after the response,
            # so count the in-flight read here -- otherwise the request
            # that first crosses the cap would still get a full listing.
            include_current=True,
        )
        guard_mode = guard["mode"]
    except Exception as e:
        logger.warning(f"[CorrectionsAPI] Extraction guard failed (non-fatal): {e}")
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
    if stale:
        if status and status != STATUS_APPROVED:
            raise HTTPException(
                status_code=400,
                detail="stale=true only applies to approved corrections",
            )
        status = STATUS_APPROVED
    corrections = manager.list(
        tenant_id=auth.tenant_id,
        status=status,
        agent_id=agent_id,
        limit=max(1, min(limit, 500)),
    )
    if stale:
        corrections = [
            c
            for c in corrections
            if is_stale(c.last_validated_at, c.updated_at, c.created_at)
        ]
    return [_to_response(c) for c in corrections]


@router.post("/corrections/{correction_id}/approve", response_model=CorrectionResponse)
async def approve_correction(
    correction_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> CorrectionResponse:
    """Approve a proposed correction. Approver is the authenticated caller.

    Approval auto-triggers three maintenance passes over the tenant's
    approved corrections (all best-effort, never failing the approval):
    error-schema extraction (recurring corrections generalize into
    reusable warnings), a contradiction scan (conflicting corrections
    become integrity flags), and a proposer->approver pair check
    (directed pairs that dominate recent approvals become integrity
    flags).
    """
    manager = _get_manager(request)
    _get_tenant_correction(manager, correction_id, auth.tenant_id)
    try:
        approved = manager.approve(correction_id, approved_by=auth.user_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    record_correction_lifecycle("approve")

    store = getattr(request.app.state, "learning_store", None) or manager.store
    if store is not None:
        try:
            from ...learning.error_schemata import extract_error_schemas

            extract_error_schemas(store, tenant_id=auth.tenant_id)
        except Exception as e:
            logger.warning(f"[CorrectionsAPI] Schema extraction failed (non-fatal): {e}")
        try:
            from ...learning.contradiction import scan_corrections

            scan_corrections(store, tenant_id=auth.tenant_id)
        except Exception as e:
            logger.warning(f"[CorrectionsAPI] Contradiction scan failed (non-fatal): {e}")
        try:
            from ...learning.approval_patterns import check_pair_dominance

            check_pair_dominance(store, tenant_id=auth.tenant_id)
        except Exception as e:
            logger.warning(f"[CorrectionsAPI] Pair check failed (non-fatal): {e}")

    return _to_response(approved)


@router.post("/corrections/{correction_id}/reject", response_model=CorrectionResponse)
async def reject_correction(
    correction_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> CorrectionResponse:
    """Reject a proposed correction (terminal)."""
    manager = _get_manager(request)
    _get_tenant_correction(manager, correction_id, auth.tenant_id)
    try:
        rejected = manager.reject(correction_id, rejected_by=auth.user_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    record_correction_lifecycle("reject")
    return _to_response(rejected)


@router.post("/corrections/{correction_id}/retire", response_model=CorrectionResponse)
async def retire_correction(
    correction_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> CorrectionResponse:
    """Retire an approved correction (stops influencing prompts)."""
    manager = _get_manager(request)
    _get_tenant_correction(manager, correction_id, auth.tenant_id)
    try:
        retired = manager.retire(correction_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    record_correction_lifecycle("retire")
    return _to_response(retired)


@router.post(
    "/corrections/{correction_id}/revalidate", response_model=CorrectionResponse
)
async def revalidate_correction(
    correction_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> CorrectionResponse:
    """Re-confirm an approved correction is still true (knowledge aging).

    Sets last_validated_at/by to the authenticated caller and now,
    resetting the staleness clock. Status is unchanged. Note the
    governance report flags corrections whose last validator is their
    own proposer (self-revalidation echoes the four-eyes concern).
    """
    manager = _get_manager(request)
    _get_tenant_correction(manager, correction_id, auth.tenant_id)
    try:
        revalidated = manager.revalidate(correction_id, validated_by=auth.user_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    record_correction_lifecycle("revalidate")
    return _to_response(revalidated)


@router.delete("/corrections/{correction_id}", response_model=ErasureResponse)
async def erase_correction_endpoint(
    correction_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> ErasureResponse:
    """GDPR Article 17 hard-delete. Daily-capped; leaves an audit event."""
    manager = _get_manager(request)
    try:
        result = erase_correction(
            manager.store,
            _validated_id(correction_id),
            tenant_id=auth.tenant_id,
            actor=auth.user_id,
        )
    except ErasureCapExceededError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except ValueError:
        raise HTTPException(status_code=404, detail="Correction not found")
    return ErasureResponse(**result)
