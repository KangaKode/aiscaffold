"""
MCP connectors API -- manage and invoke per-tenant MCP servers.

  POST   /api/v1/mcp/servers               -- Register a server
  GET    /api/v1/mcp/servers               -- List this tenant's servers
  DELETE /api/v1/mcp/servers/{name}        -- Remove a registration
  POST   /api/v1/mcp/servers/{name}/health -- Reachability + tool count
  POST   /api/v1/mcp/servers/{name}/invoke -- Call one tool (sanitized output)

Security:
  - tenant_id always comes from the AuthContext, never the request body
  - server_url passes validate_url (anti-SSRF) at the boundary
  - credentials are env var references (MCP_*), never raw tokens in requests
  - invoke responses are sanitized before they leave the platform
  - Requires app.state.mcp_registry / mcp_client (503 when not configured)

Keep this file under 250 lines.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...connectors.mcp_client import MCPClient
from ...connectors.mcp_registry import (
    SCOPE_PREFIX,
    MCPServerConfig,
    MCPServerRegistry,
)
from ...security import (
    ValidationError,
    validate_dict_size,
    validate_identifier,
    validate_length,
    validate_url,
)
from ...security.prompt_guard import sanitize_for_prompt
from ..middleware.auth import AuthContext, verify_api_key
from ..middleware.rate_limit import check_rate_limit

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_ARGUMENTS_BYTES = 10_240
MAX_INVOKE_RESPONSE_CHARS = 50_000


def _get_registry(request: Request) -> MCPServerRegistry:
    registry = getattr(request.app.state, "mcp_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=503,
            detail="MCP connectors not available (registry not configured)",
        )
    return registry


def _get_client(request: Request) -> MCPClient:
    client = getattr(request.app.state, "mcp_client", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="MCP connectors not available (client not configured)",
        )
    return client


class MCPServerRegisterRequest(BaseModel):
    """Register an MCP server for the caller's tenant."""

    name: str = Field(..., description="Unique server name within the tenant")
    server_url: str = Field(..., description="Streamable-HTTP MCP endpoint")
    scope_key: str = Field(..., description="mcp:* scope gating agent access")
    auth_type: str = Field("bearer", description="bearer | api_key | none")
    credential_env_var: str = Field(
        "", description="Env var holding the secret (must match MCP_*)"
    )
    timeout: float = 30.0
    default_tool: str = Field("", description="Tool called during enrichment")
    default_arguments: dict = Field(default_factory=dict)


class MCPServerResponse(BaseModel):
    """A registered MCP server (credential env var name is never echoed)."""

    name: str
    server_url: str
    scope_key: str
    auth_type: str
    enabled: bool
    timeout: float
    default_tool: str


class MCPHealthResponse(BaseModel):
    """Reachability check result for one registered server."""

    name: str
    healthy: bool
    tools_available: int = 0


class MCPInvokeRequest(BaseModel):
    """Call one tool on a registered server."""

    tool_name: str
    arguments: dict = Field(default_factory=dict)


class MCPInvokeResponse(BaseModel):
    """Sanitized tool output (or a truncated error message)."""

    tool_name: str
    content: str
    is_error: bool = False
    error_message: str = ""
    duration_ms: float = 0.0


def _to_response(config: MCPServerConfig) -> MCPServerResponse:
    return MCPServerResponse(
        name=config.name,
        server_url=config.server_url,
        scope_key=config.scope_key,
        auth_type=config.auth_type,
        enabled=config.enabled,
        timeout=config.timeout,
        default_tool=config.default_tool,
    )


def _find_config(
    registry: MCPServerRegistry, name: str, tenant_id: str
) -> MCPServerConfig:
    config = next(
        (c for c in registry.get_for_tenant(tenant_id) if c.name == name), None
    )
    if config is None:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    return config


@router.post("/mcp/servers", response_model=MCPServerResponse, status_code=201)
async def register_mcp_server(
    body: MCPServerRegisterRequest,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> MCPServerResponse:
    """Register an MCP server for the caller's tenant."""
    try:
        # validate_url resolves DNS (blocking getaddrinfo): off the loop.
        await asyncio.to_thread(
            validate_url, body.server_url, field_name="server_url"
        )
        validate_identifier(body.name, field_name="name")
        validate_dict_size(
            body.default_arguments,
            field_name="default_arguments",
            max_size_bytes=MAX_ARGUMENTS_BYTES,
        )
        if not body.scope_key.startswith(SCOPE_PREFIX):
            raise ValidationError(
                f"scope_key must start with '{SCOPE_PREFIX}' (got '{body.scope_key}')"
            )
        config = MCPServerConfig(
            name=body.name,
            server_url=body.server_url,
            scope_key=body.scope_key,
            tenant_id=auth.tenant_id,
            auth_type=body.auth_type,
            credential_env_var=body.credential_env_var,
            timeout=body.timeout,
            default_tool=body.default_tool,
            default_arguments=body.default_arguments,
        )
        _get_registry(request).register(config)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _to_response(config)


@router.get("/mcp/servers", response_model=list[MCPServerResponse])
async def list_mcp_servers(
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> list[MCPServerResponse]:
    """List the caller's tenant MCP servers."""
    registry = _get_registry(request)
    return [_to_response(c) for c in registry.get_for_tenant(auth.tenant_id)]


@router.delete("/mcp/servers/{name}", status_code=204)
async def delete_mcp_server(
    name: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> None:
    """Remove an MCP server registration for the caller's tenant."""
    try:
        validate_identifier(name, field_name="name")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not _get_registry(request).unregister(name, auth.tenant_id):
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")


@router.post("/mcp/servers/{name}/health", response_model=MCPHealthResponse)
async def check_mcp_server_health(
    name: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> MCPHealthResponse:
    """Reachability check: can we list the server's tools?"""
    try:
        validate_identifier(name, field_name="name")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    config = _find_config(_get_registry(request), name, auth.tenant_id)
    tools = await _get_client(request).list_tools(
        config.server_url, config.resolve_credential()
    )
    return MCPHealthResponse(
        name=config.name, healthy=len(tools) > 0, tools_available=len(tools)
    )


@router.post("/mcp/servers/{name}/invoke", response_model=MCPInvokeResponse)
async def invoke_mcp_tool(
    name: str,
    body: MCPInvokeRequest,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> MCPInvokeResponse:
    """Call one tool on a registered server; output is sanitized."""
    try:
        validate_identifier(name, field_name="name")
        # Tool names may contain dots/slashes across servers, so only bound
        # the length rather than restricting the charset.
        validate_length(body.tool_name, "tool_name", min_length=1, max_length=200)
        validate_dict_size(
            body.arguments, field_name="arguments", max_size_bytes=MAX_ARGUMENTS_BYTES
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    config = _find_config(_get_registry(request), name, auth.tenant_id)
    result = await _get_client(request).call_tool(
        server_url=config.server_url,
        tool_name=body.tool_name,
        arguments=body.arguments,
        tenant_id=auth.tenant_id,
        auth_token=config.resolve_credential(),
        timeout=config.timeout,
    )
    return MCPInvokeResponse(
        tool_name=result.tool_name,
        content=sanitize_for_prompt(result.content, max_length=MAX_INVOKE_RESPONSE_CHARS),
        is_error=result.is_error,
        error_message=result.error_message,
        duration_ms=result.duration_ms,
    )
