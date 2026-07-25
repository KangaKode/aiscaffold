"""
MCP tool caller -- the platform's only path to MCP servers.

Agents never call MCP directly: the platform controls the call path so
every call is URL-validated (anti-SSRF), credential-resolved, timed, and
audit-logged, and every response is sanitized before it can reach a
prompt (see orchestration/mcp_enrichment.py).

Uses the official `mcp` Python SDK (streamable-http transport), installed
via the optional extra: pip install '<project>[mcp]'. A custom transport
can be injected for tests or bespoke protocols -- anything with
``call_tool`` / ``list_tools`` coroutines matching MCPTransport.

Keep this file under 250 lines.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..security.validators import validate_url

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_ERROR_MESSAGE_CHARS = 200


@dataclass
class MCPToolResult:
    """Result of a single MCP tool call."""

    content: str
    tool_name: str
    is_error: bool = False
    error_message: str = ""
    duration_ms: float = 0.0


@dataclass
class MCPToolInfo:
    """Metadata about an available MCP tool."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


class MCPTransport(Protocol):
    """Minimal transport interface (implemented by the SDK adapter and stubs)."""

    async def call_tool(
        self,
        server_url: str,
        tool_name: str,
        arguments: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> str:
        """Call a tool and return its text content."""
        ...

    async def list_tools(
        self, server_url: str, headers: dict[str, str], timeout: float
    ) -> list[MCPToolInfo]:
        """List the tools a server exposes."""
        ...


class _SdkTransport:
    """Default transport backed by the official `mcp` SDK (lazy import)."""

    async def call_tool(
        self,
        server_url: str,
        tool_name: str,
        arguments: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> str:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
            server_url, headers=headers, timeout=timeout
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
        parts = [block.text for block in result.content if hasattr(block, "text")]
        return "\n".join(parts)

    async def list_tools(
        self, server_url: str, headers: dict[str, str], timeout: float
    ) -> list[MCPToolInfo]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
            server_url, headers=headers, timeout=timeout
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_result = await session.list_tools()
        return [
            MCPToolInfo(
                name=t.name,
                description=t.description or "",
                input_schema=getattr(t, "inputSchema", None) or {},
            )
            for t in tools_result.tools
        ]


def _truncate_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:MAX_ERROR_MESSAGE_CHARS]}"


class MCPClient:
    """Calls MCP servers and returns structured, never-raising results.

    ``call_tool`` returns an MCPToolResult with ``is_error=True`` instead of
    raising, so callers (the enrichment pipeline, the invoke API) stay
    non-fatal by construction. Only URL validation raises -- an unsafe URL
    is a caller bug, not a runtime condition.
    """

    def __init__(
        self,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: MCPTransport | None = None,
    ) -> None:
        self.default_timeout = default_timeout
        self._transport = transport or _SdkTransport()

    @staticmethod
    def _headers(auth_token: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

    async def call_tool(
        self,
        server_url: str,
        tool_name: str,
        arguments: dict[str, Any],
        tenant_id: str,
        auth_token: str | None = None,
        timeout: float | None = None,
    ) -> MCPToolResult:
        """Call an MCP tool; audit-log the outcome (metadata only)."""
        # validate_url resolves DNS (blocking getaddrinfo) -- off the loop.
        await asyncio.to_thread(validate_url, server_url, field_name="server_url")
        effective_timeout = timeout or self.default_timeout
        start = time.monotonic()
        status = "success"

        try:
            content = await self._transport.call_tool(
                server_url,
                tool_name,
                arguments,
                self._headers(auth_token),
                effective_timeout,
            )
            result = MCPToolResult(
                content=content,
                tool_name=tool_name,
                duration_ms=(time.monotonic() - start) * 1000,
            )
        except TimeoutError:
            status = "timeout"
            result = MCPToolResult(
                content="",
                tool_name=tool_name,
                is_error=True,
                error_message=f"Timeout after {effective_timeout}s",
                duration_ms=(time.monotonic() - start) * 1000,
            )
        except Exception as exc:
            status = "error"
            result = MCPToolResult(
                content="",
                tool_name=tool_name,
                is_error=True,
                error_message=_truncate_error(exc),
                duration_ms=(time.monotonic() - start) * 1000,
            )

        # Metadata-only audit line: never log arguments or response content.
        logger.info(
            "[MCPAudit] tool_call tool=%s tenant_id=%s status=%s duration_ms=%.1f",
            tool_name, tenant_id, status, result.duration_ms,
        )
        return result

    async def list_tools(
        self,
        server_url: str,
        auth_token: str | None = None,
        *,
        config: Any | None = None,
        flag_hook: Any | None = None,
        report_out: dict[str, int] | None = None,
    ) -> list[MCPToolInfo]:
        """List available tools on an MCP server ([] on any failure).

        After a successful fetch, tool descriptions/schemas are screened
        detect-only (connectors/tool_screen.py). Optional ``config`` enables
        hash memory; ``flag_hook`` receives findings (api/ wires store flags);
        ``report_out`` receives advisory counts. List is never filtered.
        """
        # validate_url resolves DNS (blocking getaddrinfo) -- off the loop.
        await asyncio.to_thread(validate_url, server_url, field_name="server_url")
        try:
            tools = await self._transport.list_tools(
                server_url, self._headers(auth_token), self.default_timeout
            )
        except Exception as exc:
            logger.warning(
                "[MCPClient] list_tools failed for %s: %s",
                server_url, _truncate_error(exc),
            )
            return []

        from .tool_screen import screen_listed_tools

        try:
            await asyncio.to_thread(
                screen_listed_tools, tools, config, flag_hook, report_out
            )
        except Exception:
            logger.warning(
                "[MCPClient] tool metadata screen failed (fail-open)",
                exc_info=True,
            )
            if report_out is not None:
                report_out["metadata_screen_failed"] = 1
        return tools

    async def health_check(
        self,
        server_url: str,
        auth_token: str | None = None,
    ) -> bool:
        """True when the server is reachable and exposes at least one tool."""
        return len(await self.list_tools(server_url, auth_token)) > 0
