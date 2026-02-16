"""
Health, readiness, and metrics endpoints.

  GET /health       -- Liveness probe (always returns 200 if process is alive)
  GET /health/ready -- Readiness probe (checks DB, agents)
  GET /metrics      -- Basic operational metrics
"""

import logging
import time

from fastapi import APIRouter, Depends, Request

from ..middleware.auth import AuthContext, verify_api_key
from ..models.responses import HealthResponse, MetricsResponse, ReadinessResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def liveness(request: Request) -> HealthResponse:
    """Liveness probe -- returns 200 if the process is running."""
    registry = request.app.state.registry
    start_time = getattr(request.app.state, "start_time", time.time())
    healthy_count = sum(
        1 for e in registry.get_all_entries() if e.healthy
    )
    return HealthResponse(
        status="healthy",
        agents_registered=registry.count,
        agents_healthy=healthy_count,
        uptime_seconds=round(time.time() - start_time, 1),
    )


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness(request: Request) -> ReadinessResponse:
    """Readiness probe -- checks that dependencies are available."""
    registry = request.app.state.registry
    checks = {"agents_registered": registry.count > 0}

    if registry.remote_count > 0:
        health_results = await registry.health_check_all()
        checks["remote_agents_healthy"] = all(health_results.values())
    else:
        checks["remote_agents_healthy"] = True

    all_ready = all(checks.values())
    return ReadinessResponse(ready=all_ready, checks=checks)


@router.get("/metrics", response_model=MetricsResponse)
async def metrics(
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> MetricsResponse:
    """Basic operational metrics."""
    registry = request.app.state.registry
    m = request.app.state.metrics
    completed = m["tasks_completed"]
    avg_duration = (
        m["total_duration"] / completed if completed > 0 else 0.0
    )
    return MetricsResponse(
        tasks_completed=completed,
        tasks_failed=m["tasks_failed"],
        average_duration_seconds=round(avg_duration, 2),
        agents_registered=registry.count,
        total_agent_calls=m["total_agent_calls"],
    )
