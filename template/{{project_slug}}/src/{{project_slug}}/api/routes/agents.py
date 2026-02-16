"""
Agent registry API -- register, list, inspect, and remove agents.

  POST   /api/v1/agents       -- Register a new agent (local or remote)
  GET    /api/v1/agents       -- List all registered agents
  GET    /api/v1/agents/{id}  -- Get agent details + health
  DELETE /api/v1/agents/{id}  -- Unregister an agent
  POST   /api/v1/agents/health -- Run health checks on all remote agents

Security:
  - Agent base_url is validated against SSRF (no private IPs, no file://)
  - Agent name must be a safe identifier
  - Capabilities list is size-limited
  - All mutation endpoints require API key
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ...security import (
    ValidationError,
    validate_identifier,
    validate_list_size,
    validate_url,
)
from ..middleware.auth import verify_api_key
from ..middleware.rate_limit import check_rate_limit
from ..models.requests import AgentRegistration
from ..models.responses import AgentInfo, AgentListResponse

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_CAPABILITIES = 50


@router.post("/agents", response_model=AgentInfo)
async def register_agent(
    registration: AgentRegistration,
    request: Request,
    _key: str | None = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> AgentInfo:
    """Register a remote agent. It must implement /analyze, /challenge, /vote."""
    registry = request.app.state.registry

    try:
        validate_identifier(registration.name, "agent name")
        validate_url(registration.base_url, "base_url")
        validate_list_size(
            registration.capabilities, "capabilities", max_items=MAX_CAPABILITIES
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if registry.get(registration.name) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Agent '{registration.name}' already registered. "
            f"Unregister first with DELETE /api/v1/agents/{registration.name}",
        )

    agent = registry.register_remote(
        name=registration.name,
        domain=registration.domain,
        base_url=registration.base_url,
        api_key=registration.api_key,
        capabilities=registration.capabilities,
        mode=registration.mode,
    )
    logger.info(f"[AgentsAPI] Registered: {registration.name} at {registration.base_url}")
    return AgentInfo(
        name=agent.name,
        domain=agent.domain,
        agent_type="remote",
        base_url=registration.base_url,
        capabilities=registration.capabilities,
        mode=registration.mode,
    )


@router.get("/agents", response_model=AgentListResponse)
async def list_agents(request: Request) -> AgentListResponse:
    """List all registered agents with their status."""
    registry = request.app.state.registry
    agents_info = [
        AgentInfo(**entry) for entry in registry.list_info()
    ]
    return AgentListResponse(agents=agents_info, total=registry.count)


@router.get("/agents/{agent_id}", response_model=AgentInfo)
async def get_agent(agent_id: str, request: Request) -> AgentInfo:
    """Get detailed info about a specific agent."""
    registry = request.app.state.registry
    entry = registry.get_entry(agent_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return AgentInfo(**entry.to_dict())


@router.delete("/agents/{agent_id}")
async def unregister_agent(
    agent_id: str,
    request: Request,
    _key: str | None = Depends(verify_api_key),
) -> dict:
    """Remove an agent from the registry."""
    registry = request.app.state.registry
    if not registry.unregister(agent_id):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    logger.info(f"[AgentsAPI] Unregistered: {agent_id}")
    return {"status": "removed", "agent": agent_id}


@router.post("/agents/health")
async def health_check_all(request: Request) -> dict:
    """Run health checks on all remote agents."""
    registry = request.app.state.registry
    results = await registry.health_check_all()
    return {"results": results, "all_healthy": all(results.values())}
