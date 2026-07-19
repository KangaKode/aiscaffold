"""
Corrections API -- the full correction lifecycle over HTTP.

Endpoints: POST /corrections (propose), POST /corrections/{id}/supersede
(propose a successor), GET /corrections (?status, ?agent_id, ?stale,
?currently_valid), and per-id POST /approve, /reject, /retire,
/revalidate plus DELETE /{id} (GDPR Art. 17).

API-first: the only write path a non-Python client needs to participate
in the learning loop. Manager expected at app.state.corrections_manager
(503 when absent).

Security: API key on every endpoint; tenant from AuthContext (never
request body); created_by / approved_by from the authenticated caller;
propose + supersede run the same defense stack (PII redaction, content
policy, override detector). Status-transition losses -> 409. Approving
a successor when the store lacks update_if -> 501 BEFORE any status
change (successor stays proposed); the compensator surfaces a lost race
as 409 with a supersession_partial_failure integrity flag. Erasing an
ancestor with a live successor -> 409 naming the blockers; store-down
on that check -> 503 (fail closed rather than pretend "no blockers").

Store calls run via asyncio.to_thread so the loop stays responsive.

Keep this file under 520 lines.
"""

import asyncio
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...learning.aging import freshness_timestamp, is_stale
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
from ...learning.supersession import (
    MissingUpdateIfError,
    SupersessionConflictError,
)
from ...observability.metrics import record_correction_lifecycle
from ...security import ValidationError, validate_identifier, validate_in_choices
from ..middleware.auth import AuthContext, verify_api_key

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_CLAIM_CHARS = 4000
MAX_REASON_CHARS = 2000
VALID_STATUSES = [STATUS_PROPOSED, STATUS_APPROVED, STATUS_REJECTED, STATUS_RETIRED]
# Correction ids are short uuid prefixes; anything else 404s without a store hit.
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
    """A correction as returned by the API (validity fields '' on pre-B7 rows)."""

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
                "Corrections are not enabled: no corrections manager is "
                "configured (app.state.corrections_manager)"
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
        valid_at=correction.valid_at,
        invalid_at=correction.invalid_at,
        supersedes_id=correction.supersedes_id,
        created_at=correction.created_at,
        updated_at=correction.updated_at,
        override_flags=override_flags or [],
    )


def _validated_id(correction_id: str) -> str:
    if not _CORRECTION_ID_RE.match(correction_id):
        raise HTTPException(status_code=404, detail="Correction not found")
    return correction_id


async def _get_tenant_correction(
    manager: CorrectionsManager, correction_id: str, tenant_id: str
) -> Correction:
    """404 for missing AND cross-tenant ids -- no existence leak."""
    correction = await asyncio.to_thread(manager.get, _validated_id(correction_id))
    if correction is None or correction.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Correction not found")
    return correction


async def _run_override_screen(
    request: Request, correction: Correction, tenant_id: str
) -> list[str]:
    """Screen a newly proposed correction; never fail the write."""
    detector = getattr(request.app.state, "override_detector", None)
    if detector is None:
        return []
    try:
        return await asyncio.to_thread(
            detector.screen_and_flag, correction, tenant_id=tenant_id
        )
    except Exception as e:
        logger.warning(f"[CorrectionsAPI] Override screening failed: {e}")
        return []


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
        )
    except ValueError as e:
        # Content-policy rejection: refuse the write, tell the caller why.
        raise HTTPException(status_code=422, detail=str(e))
    record_correction_lifecycle("propose")
    override_flags = await _run_override_screen(request, correction, auth.tenant_id)
    return _to_response(correction, override_flags)


@router.post(
    "/corrections/{correction_id}/supersede",
    response_model=CorrectionResponse,
    status_code=201,
)
async def supersede_correction(
    correction_id: str,
    proposal: CorrectionProposal,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> CorrectionResponse:
    """Propose a successor row that will replace {correction_id} on approve.

    Creates a proposed correction with supersedes_id={correction_id};
    ancestor invalidation happens only when the successor is approved
    (see supersession.py). Same defense stack as POST /corrections
    (content policy + PII in manager, override detector after). 404 for
    missing / cross-tenant (no existence leak); 409 when ancestor is
    not currently-valid approved.
    """
    manager = _get_manager(request)
    ancestor = await _get_tenant_correction(manager, correction_id, auth.tenant_id)
    try:
        validate_identifier(proposal.agent_id, "agent_id")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
        )
    except ValueError as e:
        message = str(e)
        if "not currently-valid" in message or "not found" in message:
            raise HTTPException(status_code=409, detail=message)
        raise HTTPException(status_code=422, detail=message)
    record_correction_lifecycle("propose_supersession")
    override_flags = await _run_override_screen(request, successor, auth.tenant_id)
    return _to_response(successor, override_flags)


@router.get("/corrections", response_model=list[CorrectionResponse])
async def list_corrections(
    request: Request,
    status: str = "",
    agent_id: str = "",
    limit: int = 100,
    stale: bool = False,
    currently_valid: bool | None = None,
    auth: AuthContext = Depends(verify_api_key),
) -> list[CorrectionResponse]:
    """List this tenant's corrections, newest first.

    currently_valid=true keeps invalid_at=''; false keeps only history.
    stale=true restricts to APPROVED rows older than CORRECTION_STALE_DAYS
    (freshness = last_validated_at, else updated_at), sorted stalest-first
    over a capped 500-row fetch.

    Intersection (see PLATFORM_GUIDE): stale=true defaults to currently
    valid (invalidated ancestors excluded from the review queue);
    stale=true&currently_valid=false surfaces invalidated history.

    Extraction guard runs on every call (detection-only by default;
    EXTRACTION_GUARD_ENFORCE=true returns 429 + Retry-After).
    """
    manager = _get_manager(request)
    guard_mode = ""
    try:
        guard = await asyncio.to_thread(
            evaluate_extraction_mode,
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
    # Intersection: stale=true defaults to currently-valid.
    effective_valid = True if (stale and currently_valid is None) else currently_valid
    page_limit = max(1, min(limit, 500))
    corrections = await asyncio.to_thread(
        manager.list,
        tenant_id=auth.tenant_id,
        status=status,
        agent_id=agent_id,
        # Stale review queue fetches the full 500-row horizon from the
        # stale end and pages in Python (see docstring for caveats).
        limit=500 if stale else page_limit,
        order_by="updated_at ASC" if stale else "created_at DESC",
    )
    if effective_valid is True:
        corrections = [c for c in corrections if not c.invalid_at]
    elif effective_valid is False:
        corrections = [c for c in corrections if c.invalid_at]
    if stale:
        # Sort by freshness key (last_validated_at, else updated_at) so a
        # long-ago-revalidated row still pages before newer ones.
        corrections.sort(
            key=lambda c: freshness_timestamp(
                c.last_validated_at, c.updated_at, c.created_at
            )
        )
        corrections = [
            c for c in corrections
            if is_stale(c.last_validated_at, c.updated_at, c.created_at)
        ][:page_limit]
    return [_to_response(c) for c in corrections]


@router.post("/corrections/{correction_id}/approve", response_model=CorrectionResponse)
async def approve_correction(
    correction_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> CorrectionResponse:
    """Approve a proposed correction. Approver is the authenticated caller.

    Auto-triggers best-effort maintenance passes over the tenant
    (schema extraction, contradiction scan, proposer/approver pair
    dominance, drift check). None can fail the approval.
    """
    manager = _get_manager(request)
    await _get_tenant_correction(manager, correction_id, auth.tenant_id)
    try:
        approved = await asyncio.to_thread(
            manager.approve, correction_id, approved_by=auth.user_id
        )
    except MissingUpdateIfError as e:
        # T17: fire BEFORE any status change; successor stays proposed.
        raise HTTPException(status_code=501, detail=str(e))
    except SupersessionConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    record_correction_lifecycle("approve")

    store = getattr(request.app.state, "learning_store", None) or manager.store
    if store is not None:
        await _run_post_approve_passes(store, auth.tenant_id)
    return _to_response(approved)


async def _run_post_approve_passes(store, tenant_id: str) -> None:
    """Best-effort maintenance passes after an approval; none may fail the write."""
    from ...learning.approval_patterns import check_pair_dominance
    from ...learning.contradiction import scan_corrections
    from ...learning.error_schemata import extract_error_schemas
    from ...learning.loop_integrity import create_drift_check_hook

    passes = (
        ("schema_extraction", lambda: extract_error_schemas(store, tenant_id=tenant_id)),
        ("contradiction_scan", lambda: scan_corrections(store, tenant_id=tenant_id)),
        ("pair_check", lambda: check_pair_dominance(store, tenant_id=tenant_id)),
    )
    for name, fn in passes:
        try:
            await asyncio.to_thread(fn)
        except Exception as e:
            logger.warning(f"[CorrectionsAPI] {name} failed (non-fatal): {e}")
    try:
        hook = create_drift_check_hook(store, tenant_id=tenant_id)
        if hook is not None:
            await asyncio.to_thread(hook)
    except Exception as e:
        logger.warning(f"[CorrectionsAPI] Drift check failed (non-fatal): {e}")


@router.post("/corrections/{correction_id}/reject", response_model=CorrectionResponse)
async def reject_correction(
    correction_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> CorrectionResponse:
    """Reject a proposed correction (terminal)."""
    manager = _get_manager(request)
    await _get_tenant_correction(manager, correction_id, auth.tenant_id)
    try:
        rejected = await asyncio.to_thread(
            manager.reject, correction_id, rejected_by=auth.user_id
        )
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
    await _get_tenant_correction(manager, correction_id, auth.tenant_id)
    try:
        retired = await asyncio.to_thread(manager.retire, correction_id)
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
    """Re-confirm a currently-valid approved correction (knowledge aging).

    Sets last_validated_at/by to the caller/now; status unchanged.
    Superseded (invalidated) rows return 409. The governance report flags
    self-revalidations (validator == proposer).
    """
    manager = _get_manager(request)
    await _get_tenant_correction(manager, correction_id, auth.tenant_id)
    try:
        revalidated = await asyncio.to_thread(
            manager.revalidate, correction_id, validated_by=auth.user_id
        )
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
        result = await asyncio.to_thread(
            erase_correction,
            manager.store,
            _validated_id(correction_id),
            tenant_id=auth.tenant_id,
            actor=auth.user_id,
            checkin_manager=manager.checkin_manager,
        )
    except ErasureCapExceededError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except ErasureBlockedBySuccessorError as e:
        # Operable payload naming the blockers; erase top-down.
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(e),
                "correction_id": e.correction_id,
                "successor_ids": e.successor_ids,
            },
        )
    except RuntimeError as e:
        # Store-down on the successor check -- fail closed to 503.
        logger.warning(f"[CorrectionsAPI] Successor check failed: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError:
        raise HTTPException(status_code=404, detail="Correction not found")
    return ErasureResponse(**result)
