"""
Connectors -- optional integrations with external tool servers.

Ships an MCP (Model Context Protocol) client and a per-tenant server
registry. The platform calls MCP tools on behalf of agents -- agents
never call MCP directly -- so every call is validated, credential-scoped,
and sanitized before its output reaches a prompt.

Install the optional dependency to use the real transport:
    pip install '<project>[mcp]'
"""

from .mcp_client import MCPClient, MCPToolInfo, MCPToolResult  # noqa: F401
from .mcp_registry import MCPServerConfig, MCPServerRegistry  # noqa: F401

__all__ = [
    "MCPClient",
    "MCPToolInfo",
    "MCPToolResult",
    "MCPServerConfig",
    "MCPServerRegistry",
]
