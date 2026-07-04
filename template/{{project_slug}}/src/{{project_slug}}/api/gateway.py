"""
API Gateway -- FastAPI application factory.

Creates the FastAPI app with all routes, middleware, and dependencies.
This is the entrypoint for uvicorn:

    uvicorn {{project_slug}}.api.gateway:create_app --factory --host 0.0.0.0 --port 8000

Or for development:

    uvicorn {{project_slug}}.api.gateway:app --reload

Security:
  - CORS restricted to configured origins (default: localhost only)
  - Production auth check on startup
  - Rate limiting via middleware
  - All external input validated at boundary

Keep this file under 300 lines. Route logic lives in routes/.
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
from .routes import (
    activity,
    agents,
    audit,
    budgets,
    chat,
    checkins,
    corrections,
    feedback,
    health,
    preferences,
    round_table,
    sessions,
    webhooks,
)

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

    # Activity tracking: one activity_events row per request, plus the
    # anomaly-review API. Enabled by default (ACTIVITY_TRACKING_ENABLED=false
    # to opt out); degrades to a no-op if the learning store is unavailable,
    # so zero-config deployments keep working.
    if os.environ.get("ACTIVITY_TRACKING_ENABLED", "true").strip().lower() not in (
        "false", "0", "no",
    ):
        try:
            from ..learning.activity import ActivityTracker
            from ..learning.store import get_learning_store
            from .middleware.activity import ActivityTrackingMiddleware

            store = get_learning_store()
            application.state.learning_store = store
            application.state.activity_tracker = ActivityTracker(store)
            application.add_middleware(ActivityTrackingMiddleware)
            logger.info("[Gateway] Activity tracking enabled")
        except Exception as e:
            logger.warning(f"[Gateway] Activity tracking init failed (non-fatal): {e}")

    # Governance: budget manager (attached to the LLM client when both exist)
    # and the deliberation audit trail. Both reuse the learning store and
    # degrade to no-ops when it is unavailable.
    try:
        from ..llm.budget_manager import BudgetManager
        from ..orchestration.deliberation_audit import DeliberationAuditor

        gov_store = getattr(application.state, "learning_store", None)
        budget_manager = BudgetManager(store=gov_store)
        application.state.budget_manager = budget_manager
        if llm_client is not None:
            llm_client.budget_manager = budget_manager
        application.state.deliberation_auditor = DeliberationAuditor(store=gov_store)
        logger.info("[Gateway] Governance (budgets + audit trail) initialized")
    except Exception as e:
        logger.warning(f"[Gateway] Governance init failed (non-fatal): {e}")

    # Corrections lifecycle over HTTP (API-first): manager + override
    # detector on app.state. Reuses the learning store; degrades to 503
    # at the routes when unavailable.
    try:
        from ..learning.content_policy import ContentPolicy
        from ..learning.corrections import CorrectionsManager
        from ..learning.override_detector import OverrideDetector
        from ..learning.store import get_learning_store

        corr_store = getattr(application.state, "learning_store", None)
        if corr_store is None:
            corr_store = get_learning_store()
        application.state.corrections_manager = CorrectionsManager(
            corr_store,
            checkin_manager=getattr(application.state, "checkin_manager", None),
            content_policy=ContentPolicy(store=corr_store),
        )
        application.state.override_detector = OverrideDetector(store=corr_store)
        logger.info("[Gateway] Corrections manager initialized")
    except Exception as e:
        logger.warning(f"[Gateway] Corrections init failed (non-fatal): {e}")

    try:
        from ..learning.rag.transcript_indexer import TranscriptIndexer

        application.state.transcript_indexer = TranscriptIndexer()
        logger.info("[Gateway] Transcript indexer initialized")
    except Exception as e:
        logger.warning(f"[Gateway] Transcript indexer init failed (non-fatal): {e}")

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
    application.include_router(
        activity.router, prefix="/api/v1", tags=["Activity"]
    )
    application.include_router(
        budgets.router, prefix="/api/v1", tags=["Governance - Budgets"]
    )
    application.include_router(
        audit.router, prefix="/api/v1", tags=["Governance - Audit"]
    )
    application.include_router(
        corrections.router, prefix="/api/v1", tags=["Learning - Corrections"]
    )

    logger.info("[Gateway] API gateway initialized")
    return application


app = create_app()
