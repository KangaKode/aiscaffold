"""
Feedback API -- record and query user feedback signals.

  POST /api/v1/feedback           -- Record a feedback signal
  GET  /api/v1/feedback           -- Query signals (filters: agent, type, context, since)
  GET  /api/v1/feedback/counts    -- Get signal counts by type
  GET  /api/v1/feedback/rates     -- Get acceptance rates per agent

Security:
  - Input validation on all fields
  - Rate limiting on record endpoint
  - Auth required for mutations
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...learning.feedback_tracker import FeedbackTracker
from ...learning.models import FeedbackSignal
from ...security import ValidationError, validate_length
from ..middleware.auth import verify_api_key
from ..middleware.rate_limit import check_rate_limit

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_CONTENT_LENGTH = 50_000
ALLOWED_SIGNAL_TYPES = {"accept", "reject", "modify", "rate", "dismiss", "escalate"}


class FeedbackRequest(BaseModel):
    """Request to record a user feedback signal."""

    signal_type: str = Field(..., description="accept, reject, modify, rate, dismiss, escalate")
    context_type: str = Field("", description="E.g., chat, round_table, suggestion")
    agent_id: str = Field("", description="Which agent produced the output")
    content: str = Field("", description="The content the user reacted to")
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    metadata: dict = Field(default_factory=dict)
    session_id: str = Field("")


class FeedbackResponse(BaseModel):
    """Response after recording a feedback signal."""

    id: str
    signal_type: str
    agent_id: str
    context_type: str
    created_at: str


def _get_tracker(request: Request) -> FeedbackTracker:
    tracker = getattr(request.app.state, "feedback_tracker", None)
    if tracker is None:
        tracker = FeedbackTracker()
        request.app.state.feedback_tracker = tracker
    return tracker


@router.post("/feedback", response_model=FeedbackResponse)
async def record_feedback(
    fb: FeedbackRequest,
    request: Request,
    _key: str | None = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> FeedbackResponse:
    """Record a user feedback signal."""
    if fb.signal_type not in ALLOWED_SIGNAL_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"signal_type must be one of: {', '.join(sorted(ALLOWED_SIGNAL_TYPES))}",
        )
    try:
        validate_length(fb.signal_type, "signal_type", min_length=1, max_length=50)
        if fb.content:
            validate_length(fb.content, "content", max_length=MAX_CONTENT_LENGTH)
        if fb.agent_id:
            validate_length(fb.agent_id, "agent_id", max_length=100)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    tracker = _get_tracker(request)
    signal = FeedbackSignal(
        signal_type=fb.signal_type,
        context_type=fb.context_type,
        agent_id=fb.agent_id,
        content=fb.content,
        confidence=fb.confidence,
        metadata=fb.metadata,
        session_id=fb.session_id,
    )
    recorded = tracker.record(signal)

    trust_mgr = getattr(request.app.state, "trust_manager", None)
    if trust_mgr and signal.agent_id:
        trust_mgr.update_from_signal(signal)

    return FeedbackResponse(
        id=recorded.id,
        signal_type=recorded.signal_type,
        agent_id=recorded.agent_id,
        context_type=recorded.context_type,
        created_at=recorded.created_at,
    )


@router.get("/feedback")
async def query_feedback(
    request: Request,
    agent_id: str | None = None,
    signal_type: str | None = None,
    context_type: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> dict:
    """Query feedback signals with optional filters."""
    tracker = _get_tracker(request)
    signals = tracker.get_signals(
        agent_id=agent_id,
        signal_type=signal_type,
        context_type=context_type,
        since=since,
        limit=min(limit, 200),
    )
    return {
        "signals": [
            {
                "id": s.id,
                "signal_type": s.signal_type,
                "context_type": s.context_type,
                "agent_id": s.agent_id,
                "confidence": s.confidence,
                "created_at": s.created_at,
            }
            for s in signals
        ],
        "total": len(signals),
    }


@router.get("/feedback/counts")
async def feedback_counts(
    request: Request,
    agent_id: str | None = None,
    since: str | None = None,
) -> dict:
    """Get signal counts grouped by type."""
    tracker = _get_tracker(request)
    counts = tracker.get_signal_counts(agent_id=agent_id, since=since)
    return {"counts": counts, "total": sum(counts.values())}


@router.get("/feedback/rates")
async def acceptance_rates(
    request: Request,
    since: str | None = None,
) -> dict:
    """Get acceptance rates per agent."""
    tracker = _get_tracker(request)
    rates = tracker.get_acceptance_rates(since=since)
    return {"rates": rates}
