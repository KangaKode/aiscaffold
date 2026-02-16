"""
API Gateway -- FastAPI application factory.

Creates the FastAPI app with all routes, middleware, and dependencies.
This is the entrypoint for uvicorn:

    uvicorn src.{{project_slug}}.api.gateway:create_app --factory --host 0.0.0.0 --port 8000

Or for development:

    uvicorn src.{{project_slug}}.api.gateway:app --reload

Security:
  - CORS restricted to configured origins (default: localhost only)
  - Production auth check on startup
  - Rate limiting via middleware
  - All external input validated at boundary

Keep this file under 200 lines. Route logic lives in routes/.
"""

import logging
import os
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..agents.registry import AgentRegistry
from ..learning.agent_trust import AgentTrustManager
from ..learning.checkin_manager import CheckInManager
from ..learning.feedback_tracker import FeedbackTracker
from ..learning.schema import initialize_schema as init_learning_db
from ..learning.user_profile import UserProfileManager
from ..llm import create_client as create_llm_client
from ..orchestration.round_table import RoundTableConfig
from .middleware.auth import check_production_auth
from .routes import agents, chat, checkins, feedback, health, preferences, round_table, sessions, webhooks

logger = logging.getLogger(__name__)

_start_time: float = 0.0

DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:8000",
    "http://localhost:8080",
]


def _get_cors_origins() -> list[str]:
    """Load CORS origins from environment or use safe defaults.

    SECURITY: Rejects wildcard '*' to prevent credential leakage when
    allow_credentials=True. Use explicit origins instead.
    """
    origins_env = os.environ.get("CORS_ORIGINS", "")
    if origins_env.strip():
        origins = [o.strip() for o in origins_env.split(",") if o.strip()]
        if "*" in origins:
            logger.warning(
                "[Gateway] CORS_ORIGINS contains '*' -- replacing with defaults "
                "to prevent credential leakage with allow_credentials=True"
            )
            return DEFAULT_CORS_ORIGINS
        return origins
    return DEFAULT_CORS_ORIGINS


def create_app(
    registry: AgentRegistry | None = None,
    round_table_config: RoundTableConfig | None = None,
) -> FastAPI:
    """
    Application factory -- creates and configures the FastAPI app.

    Args:
        registry: Pre-configured agent registry (creates default if None).
        round_table_config: Round table configuration (creates default if None).
    """
    global _start_time
    _start_time = time.time()

    check_production_auth()

    from .middleware.auth import _is_production

    application = FastAPI(
        title="{{ project_name }} API",
        description="AI agent platform with round table orchestration",
        version="0.1.0",
        docs_url=None if _is_production() else "/docs",
        redoc_url=None if _is_production() else "/redoc",
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=_get_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    if registry is None:
        registry = AgentRegistry()
    if round_table_config is None:
        round_table_config = RoundTableConfig()

    try:
        llm_client = create_llm_client()
    except Exception as e:
        logger.warning(f"[Gateway] LLM client init failed (non-fatal): {e}")
        llm_client = None

    try:
        init_learning_db()
        application.state.feedback_tracker = FeedbackTracker()
        application.state.trust_manager = AgentTrustManager()
        application.state.checkin_manager = CheckInManager()
        application.state.profile_manager = UserProfileManager()
        logger.info("[Gateway] Learning system initialized")
    except Exception as e:
        logger.warning(f"[Gateway] Learning system init failed (non-fatal): {e}")

    application.state.registry = registry
    application.state.round_table_config = round_table_config
    application.state.llm_client = llm_client
    application.state.start_time = _start_time
    application.state.metrics = {
        "tasks_completed": 0,
        "tasks_failed": 0,
        "total_duration": 0.0,
        "total_agent_calls": 0,
    }

    application.include_router(health.router, tags=["Health"])
    application.include_router(
        agents.router, prefix="/api/v1", tags=["Agents"]
    )
    application.include_router(
        round_table.router, prefix="/api/v1", tags=["Round Table"]
    )
    application.include_router(
        sessions.router, prefix="/api/v1", tags=["Sessions"]
    )
    application.include_router(
        webhooks.router, prefix="/api/v1", tags=["Webhooks"]
    )
    application.include_router(
        chat.router, prefix="/api/v1", tags=["Chat"]
    )
    application.include_router(
        feedback.router, prefix="/api/v1", tags=["Learning - Feedback"]
    )
    application.include_router(
        preferences.router, prefix="/api/v1", tags=["Learning - Preferences"]
    )
    application.include_router(
        checkins.router, prefix="/api/v1", tags=["Learning - Check-ins"]
    )

    logger.info("[Gateway] API gateway initialized")
    return application


app = create_app()
