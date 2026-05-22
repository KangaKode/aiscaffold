"""
Session management API -- create, inspect, and manage threads.

  POST /api/v1/sessions           -- Create a new session thread
  GET  /api/v1/sessions/{id}      -- Get session state
  POST /api/v1/sessions/{id}/turns -- Add a turn to a session

Security:
  - Turn content validated for size limits
  - Bounded LRU session cache (prevents memory exhaustion)
  - UUID-based session IDs (not predictable/enumerable)
  - Rate limiting on session creation
"""

import logging
import uuid
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException

from ...harness.session import Item, Thread, Turn
from ...security import ValidationError, validate_length
from ..middleware.auth import AuthContext, verify_api_key
from ..middleware.rate_limit import check_rate_limit
from ..models.requests import AddTurnRequest, CreateSessionRequest
from ..models.responses import SessionResponse

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_SESSIONS = 500

_sessions: OrderedDict[str, Thread] = OrderedDict()


def _session_key(session_id: str, auth: AuthContext) -> str:
    """Bind session state to the authenticated tenant and user."""
    return f"{auth.tenant_id}:{auth.user_id}:{session_id}"


@router.post("/sessions", response_model=SessionResponse)
async def create_session(
    request: CreateSessionRequest | None = None,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> SessionResponse:
    """Create a new session thread."""
    session_id = f"session_{uuid.uuid4().hex[:16]}"
    metadata = request.metadata if request else {}
    thread = Thread(id=session_id, metadata=metadata)
    cache_key = _session_key(session_id, auth)
    _sessions[cache_key] = thread

    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)

    logger.info(f"[SessionsAPI] Created session: {session_id}")
    return SessionResponse(
        session_id=session_id,
        status=thread.status,
        turn_count=len(thread.turns),
        created_at=thread.created_at,
        metadata=thread.metadata,
    )


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    auth: AuthContext = Depends(verify_api_key),
) -> SessionResponse:
    """Get the current state of a session."""
    cache_key = _session_key(session_id, auth)
    thread = _sessions.get(cache_key)
    if thread is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    _sessions.move_to_end(cache_key)
    return SessionResponse(
        session_id=thread.id,
        status=thread.status,
        turn_count=len(thread.turns),
        created_at=thread.created_at,
        metadata=thread.metadata,
    )


@router.post("/sessions/{session_id}/turns", response_model=SessionResponse)
async def add_turn(
    session_id: str,
    request: AddTurnRequest,
    auth: AuthContext = Depends(verify_api_key),
) -> SessionResponse:
    """Add a turn (user input) to an existing session."""
    try:
        validate_length(request.content, "content", min_length=1, max_length=500_000)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    cache_key = _session_key(session_id, auth)
    thread = _sessions.get(cache_key)
    if thread is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    _sessions.move_to_end(cache_key)

    turn_id = f"turn_{len(thread.turns) + 1}"
    turn = Turn(id=turn_id)
    turn.add_item(Item(
        id=f"{turn_id}_input",
        type="message",
        content=request.content,
        status="completed",
    ))
    thread.add_turn(turn)

    logger.debug(f"[SessionsAPI] Added turn {turn_id} to {session_id}")
    return SessionResponse(
        session_id=thread.id,
        status=thread.status,
        turn_count=len(thread.turns),
        created_at=thread.created_at,
        metadata=thread.metadata,
    )


@router.get("/sessions")
async def list_sessions(
    auth: AuthContext = Depends(verify_api_key),
) -> dict:
    """List all active sessions."""
    auth_prefix = f"{auth.tenant_id}:{auth.user_id}:"
    visible_sessions = [
        thread for key, thread in _sessions.items() if key.startswith(auth_prefix)
    ]
    return {
        "sessions": [
            {
                "session_id": t.id,
                "status": t.status,
                "turn_count": len(t.turns),
                "created_at": t.created_at,
            }
            for t in visible_sessions
        ],
        "total": len(visible_sessions),
    }
