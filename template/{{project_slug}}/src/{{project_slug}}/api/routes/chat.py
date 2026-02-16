"""
Chat API -- lightweight multi-agent chat with streaming support.

  POST /api/v1/chat                -- Send a message, get a response
  POST /api/v1/chat/stream         -- Send a message, get SSE stream
  POST /api/v1/chat/clear          -- Clear conversation history
  POST /api/v1/chat/escalate       -- Escalate current topic to round table

Security:
  - Input size validation
  - Rate limiting on all endpoints
  - API key authentication
  - Prompt sanitization via ChatOrchestrator
"""

import json
import logging
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...llm import create_client
from ...orchestration.agent_router import AgentRouter
from ...orchestration.chat_orchestrator import (
    ChatOrchestrator,
    ChatResponse,
)
from ...security import ValidationError, validate_length
from ..middleware.auth import verify_api_key
from ..middleware.rate_limit import check_rate_limit

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_MESSAGE_LENGTH = 100_000
MAX_SESSIONS = 500

_orchestrators: OrderedDict[str, ChatOrchestrator] = OrderedDict()


# =============================================================================
# REQUEST / RESPONSE MODELS
# =============================================================================


class ChatMessageRequest(BaseModel):
    """Send a chat message."""

    message: str = Field(..., description="The user's message")
    session_id: str = Field(
        "default", description="Session ID for conversation continuity"
    )
    context: str = Field("", description="Optional context (e.g., user preferences)")


class ChatMessageResponse(BaseModel):
    """Response from the chat orchestrator."""

    content: str
    agents_consulted: list[str] = Field(default_factory=list)
    escalation_suggested: bool = False
    escalation_reason: str = ""
    agreement_level: float | None = None
    conflicts: list[dict] = Field(default_factory=list)
    duration_seconds: float = 0.0


class EscalateRequest(BaseModel):
    """Escalate a topic to the full round table."""

    session_id: str = Field("default")
    message: str = Field(
        "", description="Additional context for the round table (optional)"
    )


# =============================================================================
# HELPERS
# =============================================================================


def _session_key(session_id: str, api_key: str | None) -> str:
    """Bind session to authenticated user to prevent cross-session access."""
    user_id = api_key[:8] if api_key else "anon"
    return f"{user_id}:{session_id}"


def _get_or_create_orchestrator(
    session_id: str, request: Request, api_key: str | None = None
) -> ChatOrchestrator:
    """Get an existing orchestrator or create one. Bounded LRU cache."""
    key = _session_key(session_id, api_key)

    if key in _orchestrators:
        _orchestrators.move_to_end(key)
        return _orchestrators[key]

    llm = getattr(request.app.state, "llm_client", None) or create_client()
    registry = request.app.state.registry
    agent_router = AgentRouter(registry=registry)
    _orchestrators[key] = ChatOrchestrator(
        llm=llm,
        registry=registry,
        router=agent_router,
    )

    while len(_orchestrators) > MAX_SESSIONS:
        _orchestrators.popitem(last=False)

    logger.debug(f"[ChatAPI] Created orchestrator for session {key}")
    return _orchestrators[key]


# =============================================================================
# ROUTES
# =============================================================================


@router.post("/chat", response_model=ChatMessageResponse)
async def send_message(
    chat_request: ChatMessageRequest,
    request: Request,
    _key: str | None = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> ChatMessageResponse:
    """
    Send a chat message and get a multi-agent response.

    The orchestrator selects relevant specialists, consults them,
    cross-checks their responses, and synthesizes a final answer.
    """
    try:
        validate_length(
            chat_request.message, "message",
            min_length=1, max_length=MAX_MESSAGE_LENGTH
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    orchestrator = _get_or_create_orchestrator(chat_request.session_id, request, _key)

    trust_scores = None
    trust_mgr = getattr(request.app.state, "trust_manager", None)
    if trust_mgr:
        trust_scores = trust_mgr.get_all_scores()

    profile_context = chat_request.context
    profile_mgr = getattr(request.app.state, "profile_manager", None)
    if profile_mgr and not profile_context:
        profile_context = profile_mgr.get_context_bundle(query=chat_request.message)

    chat_response: ChatResponse = await orchestrator.chat(
        message=chat_request.message,
        trust_scores=trust_scores,
        context=profile_context,
    )

    return ChatMessageResponse(
        content=chat_response.content,
        agents_consulted=chat_response.agents_consulted,
        escalation_suggested=chat_response.escalation_suggested,
        escalation_reason=chat_response.escalation_reason,
        agreement_level=(
            chat_response.cross_check.agreement_level
            if chat_response.cross_check
            else None
        ),
        conflicts=(
            chat_response.cross_check.conflicts
            if chat_response.cross_check
            else []
        ),
        duration_seconds=chat_response.duration_seconds,
    )


@router.post("/chat/stream")
async def send_message_stream(
    chat_request: ChatMessageRequest,
    request: Request,
    _key: str | None = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> StreamingResponse:
    """
    Send a chat message and get a Server-Sent Events stream.

    Events:
      - status: Phase updates ("routing", "consulting", "cross-checking", "synthesizing")
      - agents: Which agents are being consulted
      - content: The final response content
      - metadata: Escalation info, agreement level, duration
      - done: Stream complete
    """
    try:
        validate_length(
            chat_request.message, "message",
            min_length=1, max_length=MAX_MESSAGE_LENGTH
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    orchestrator = _get_or_create_orchestrator(chat_request.session_id, request, _key)

    trust_scores = None
    trust_mgr = getattr(request.app.state, "trust_manager", None)
    if trust_mgr:
        trust_scores = trust_mgr.get_all_scores()

    profile_context = chat_request.context
    profile_mgr = getattr(request.app.state, "profile_manager", None)
    if profile_mgr and not profile_context:
        profile_context = profile_mgr.get_context_bundle(query=chat_request.message)

    async def event_generator():
        yield _sse_event("status", {"phase": "routing"})

        chat_response: ChatResponse = await orchestrator.chat(
            message=chat_request.message,
            trust_scores=trust_scores,
            context=profile_context,
        )

        if chat_response.agents_consulted:
            yield _sse_event("agents", {
                "consulted": chat_response.agents_consulted,
            })

        yield _sse_event("status", {"phase": "complete"})

        yield _sse_event("content", {"text": chat_response.content})

        yield _sse_event("metadata", {
            "escalation_suggested": chat_response.escalation_suggested,
            "escalation_reason": chat_response.escalation_reason,
            "agreement_level": (
                chat_response.cross_check.agreement_level
                if chat_response.cross_check
                else None
            ),
            "duration_seconds": chat_response.duration_seconds,
        })

        yield _sse_event("done", {})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/clear")
async def clear_history(
    session_id: str = "default",
    _key: str | None = Depends(verify_api_key),
) -> dict:
    """Clear conversation history for a session."""
    key = _session_key(session_id, _key)
    if key in _orchestrators:
        _orchestrators[key].clear_history()
        logger.info(f"[ChatAPI] Cleared history for session {key}")
    return {"status": "cleared", "session_id": session_id}


@router.post("/chat/escalate")
async def escalate_to_round_table(
    escalate_request: EscalateRequest,
    request: Request,
    _key: str | None = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> dict:
    """
    Escalate the current conversation topic to the full round table.

    Returns a redirect to the round table task endpoint with the
    conversation context pre-filled.
    """
    key = _session_key(escalate_request.session_id, _key)
    orchestrator = _orchestrators.get(key)
    if orchestrator is None:
        raise HTTPException(
            status_code=404,
            detail=f"No active chat session: {escalate_request.session_id}",
        )

    history = orchestrator._conversation_history
    context_summary = "\n".join(
        f"{h['role']}: {str(h['content'])[:300]}"
        for h in history[-10:]
    )

    task_content = (
        f"Escalated from chat session.\n\n"
        f"Conversation context:\n{context_summary}"
    )
    if escalate_request.message:
        task_content += f"\n\nAdditional context: {escalate_request.message}"

    return {
        "status": "escalated",
        "round_table_task": {
            "content": task_content,
            "context": {"escalated_from": escalate_request.session_id},
        },
        "instruction": "POST this task to /api/v1/round-table/tasks",
    }


# =============================================================================
# SSE HELPERS
# =============================================================================


def _sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event."""
    return f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"
