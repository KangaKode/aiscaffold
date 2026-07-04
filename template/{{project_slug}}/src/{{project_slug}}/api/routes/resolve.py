"""
Resolve API -- Tier 1 single-shot resolution.

  POST /api/v1/resolve  -- Answer a query in one cheap, enforced LLM call,
                           or signal escalation to chat.

This restores three-tier API parity: resolve (cheapest) -> chat ->
round table. A caller can try /resolve first and, when the response says
`escalated: true`, fall back to POST /api/v1/chat. Single-shot only
answers queries the platform has already learned (approved corrections),
and enforcement is mandatory, so a cheap answer is never a low-quality one.

Security:
  - API key required; tenant comes from AuthContext, not the body
  - Input length validated; the query is wrapped before it reaches the model
  - Rate limited like the other generative endpoints
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...llm import create_client
from ...orchestration.single_shot import resolve_single_shot
from ...security import ValidationError, validate_identifier, validate_length
from ..middleware.auth import AuthContext, verify_api_key
from ..middleware.rate_limit import check_rate_limit

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_QUERY_LENGTH = 100_000


class ResolveRequest(BaseModel):
    """Ask for a single-shot resolution."""

    query: str = Field(..., description="The query to resolve")
    agent_id: str = Field("", description="Optional agent whose corrections to use")


class ResolveResponse(BaseModel):
    """Single-shot outcome. When escalated, content is empty."""

    content: str
    tier: str = "single_shot"
    escalated: bool = False
    escalation_reason: str = ""
    enforcement_result: str = ""
    enforcement_violations: list[str] = Field(default_factory=list)
    evidence_level: str = ""
    citations_count: int = 0
    duration_seconds: float = 0.0


@router.post("/resolve", response_model=ResolveResponse)
async def resolve(
    resolve_request: ResolveRequest,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> ResolveResponse:
    """Resolve a query in one cheap, enforced call -- or escalate to chat."""
    try:
        validate_length(
            resolve_request.query, "query", min_length=1, max_length=MAX_QUERY_LENGTH
        )
        if resolve_request.agent_id:
            validate_identifier(resolve_request.agent_id, "agent_id")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    llm = getattr(request.app.state, "llm_client", None) or create_client()
    corrections_manager = getattr(request.app.state, "corrections_manager", None)

    result = await resolve_single_shot(
        query=resolve_request.query,
        llm=llm,
        corrections_manager=corrections_manager,
        learning_store=getattr(request.app.state, "learning_store", None),
        tenant_id=auth.tenant_id,
        agent_id=resolve_request.agent_id,
    )

    return ResolveResponse(
        content=result.content,
        tier=result.tier,
        escalated=result.escalated,
        escalation_reason=result.escalation_reason,
        enforcement_result=result.enforcement_result,
        enforcement_violations=result.enforcement_violations,
        evidence_level=result.evidence_level,
        citations_count=result.citations_count,
        duration_seconds=result.duration_seconds,
    )
