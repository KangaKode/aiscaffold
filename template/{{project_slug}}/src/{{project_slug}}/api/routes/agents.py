"""
Agent registry API -- register, list, inspect, and remove agents.

  POST   /api/v1/agents       -- Register a new agent (local or remote)
  GET    /api/v1/agents       -- List all registered agents
  GET    /api/v1/agents/{id}  -- Get agent details + health
  DELETE /api/v1/agents/{id}  -- Unregister an agent
  POST   /api/v1/agents/health -- Run health checks on all remote agents
  POST   /api/v1/agents/{id}/credentials/rotate -- Re-issue identity token
  DELETE /api/v1/agents/{id}/credentials -- Revoke identity token
  POST   /api/v1/agents/{id}/suspend   -- Suspend agent (blocked at dispatch)
  POST   /api/v1/agents/{id}/unsuspend -- Lift suspension

Security:
  - Agent base_url is validated against SSRF (no private IPs, no file://)
  - Agent name must be a safe identifier
  - Capabilities list is size-limited
  - All mutation endpoints require API key
  - Every operation is scoped to the caller's tenant (auth.tenant_id):
    listings show only the caller's tenant's agents, and lookups of
    another tenant's agent return 404 (never 403 -- existence of agents
    in other tenants must not leak). Single-tenant deployments run
    entirely in the "default" tenant, unchanged.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ...security import (
    ValidationError,
    validate_identifier,
    validate_in_choices,
    validate_list_size,
    validate_url,
)
from ..middleware.auth import AuthContext, verify_api_key
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
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> AgentInfo:
    """Register a remote agent. It must implement /analyze, /challenge, /vote."""
    registry = request.app.state.registry

    try:
        validate_identifier(registration.name, "agent name")
        # validate_url resolves DNS (blocking getaddrinfo): off the loop.
        await asyncio.to_thread(validate_url, registration.base_url, "base_url")
        validate_list_size(
            registration.capabilities, "capabilities", max_items=MAX_CAPABILITIES
        )
        validate_list_size(
            registration.access_scopes, "access_scopes", max_items=MAX_CAPABILITIES
        )
        validate_in_choices(
            registration.visibility, ["public", "team", "private"], "visibility"
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if registry.get(registration.name, tenant_id=auth.tenant_id) is not None:
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
        access_scopes=registration.access_scopes,
        max_calls_per_hour=registration.max_calls_per_hour,
        is_meta_agent=registration.is_meta_agent,
        visibility=registration.visibility,
        tenant_id=auth.tenant_id,
    )
    logger.info(f"[AgentsAPI] Registered: {registration.name} at {registration.base_url}")
    return AgentInfo(
        name=agent.name,
        domain=agent.domain,
        agent_type="remote",
        base_url=registration.base_url,
        capabilities=registration.capabilities,
        visibility=registration.visibility,
        tenant_id=auth.tenant_id,
    )


@router.get("/agents", response_model=AgentListResponse)
async def list_agents(
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> AgentListResponse:
    """List the caller's tenant's registered agents with their status."""
    registry = request.app.state.registry
    agents_info = [
        AgentInfo(**entry) for entry in registry.list_info(tenant_id=auth.tenant_id)
    ]
    return AgentListResponse(agents=agents_info, total=len(agents_info))


@router.get("/agents/{agent_id}", response_model=AgentInfo)
async def get_agent(
    agent_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> AgentInfo:
    """Get detailed info about one of the caller's tenant's agents."""
    registry = request.app.state.registry
    entry = registry.get_entry(agent_id, tenant_id=auth.tenant_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return AgentInfo(**entry.to_dict())


@router.delete("/agents/{agent_id}")
async def unregister_agent(
    agent_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> dict:
    """Remove one of the caller's tenant's agents from the registry."""
    registry = request.app.state.registry
    if not registry.unregister(agent_id, tenant_id=auth.tenant_id):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    logger.info(f"[AgentsAPI] Unregistered: {agent_id}")
    return {"status": "removed", "agent": agent_id}


@router.post("/agents/health")
async def health_check_all(
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> dict:
    """Run health checks on the caller's tenant's remote agents."""
    registry = request.app.state.registry
    results = await registry.health_check_all(tenant_id=auth.tenant_id)
    return {"results": results, "all_healthy": all(results.values())}


@router.post("/agents/{agent_id}/credentials/rotate")
async def rotate_agent_credentials(
    agent_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> dict:
    """Re-issue an agent's identity token. The raw token is shown ONCE.

    Tenant-scoped: rotating another tenant's agent is a 404, so a valid
    caller can never mint an identity token outside their own tenant.
    """
    registry = request.app.state.registry
    token = registry.rotate_credentials(agent_id, tenant_id=auth.tenant_id)
    if token is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    logger.info(
        f"[AgentsAPI] Rotated credentials: {agent_id} (tenant={auth.tenant_id})"
    )
    return {"identity_token": token, "note": "store securely; shown once"}


@router.delete("/agents/{agent_id}/credentials")
async def revoke_agent_credentials(
    agent_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> dict:
    """Revoke an agent's identity token (remote agents blocked at dispatch)."""
    registry = request.app.state.registry
    if not registry.revoke_credentials(agent_id, tenant_id=auth.tenant_id):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    logger.info(
        f"[AgentsAPI] Revoked credentials: {agent_id} (tenant={auth.tenant_id})"
    )
    return {"status": "revoked", "agent": agent_id}


@router.post("/agents/{agent_id}/suspend")
async def suspend_agent(
    agent_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> dict:
    """Suspend an agent -- excluded from dispatch and tenant listings."""
    registry = request.app.state.registry
    if not registry.suspend(agent_id, tenant_id=auth.tenant_id):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    logger.info(f"[AgentsAPI] Suspended: {agent_id} (tenant={auth.tenant_id})")
    return {"status": "suspended", "agent": agent_id}


@router.post("/agents/{agent_id}/unsuspend")
async def unsuspend_agent(
    agent_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> dict:
    """Lift an agent's suspension."""
    registry = request.app.state.registry
    if not registry.unsuspend(agent_id, tenant_id=auth.tenant_id):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    logger.info(f"[AgentsAPI] Unsuspended: {agent_id} (tenant={auth.tenant_id})")
    return {"status": "active", "agent": agent_id}
