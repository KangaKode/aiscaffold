"""
MCP enrichment -- bridges the MCP client into the task pipeline, non-fatally.

Before a round table dispatches, tasks whose agents declare ``mcp:*``
access scopes can be enriched with data from the matching registered MCP
servers. The platform makes the calls (agents never talk to MCP directly),
and every failure is a logged warning, never an exception -- an MCP outage
degrades a task's context, it does not kill the task.

Scope gating: each server's response lands in the task context under its
``scope_key`` (e.g. "mcp:ticket_system"), so the existing ScopeFilter
(orchestration/scope_filter.py) only shows it to agents whose
AgentCapability.access_scopes include that key.

Injection defense: MCP responses are untrusted external content. They are
sanitized first (sanitize_for_prompt truncation to MAX_RESPONSE_CHARS --
oversized hostile responses never buy scan CPU), then scanned with the
full Layer 1+2 pipeline off the event loop (detect_injection_attempt with
advanced=True: static patterns plus homoglyph/invisible-char/encoding
decoding), and boundary-wrapped (wrap_user_content) before entering any
context. Detection is log-only on this surface: benign encoded blobs
(data URIs, JWTs, base64 configs) are common in tool output, so a
finding never blocks the enrichment.

Keep this file under 200 lines.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..connectors.mcp_client import MCPClient
from ..connectors.mcp_registry import SCOPE_PREFIX, MCPServerRegistry
from ..security.prompt_guard import (
    detect_injection_attempt,
    sanitize_for_prompt,
    wrap_user_content,
)

logger = logging.getLogger(__name__)

MAX_RESPONSE_CHARS = 50_000


def collect_mcp_scopes(agents: list[Any]) -> set[str]:
    """The mcp:* scopes declared by any of the given agents' capabilities."""
    scopes: set[str] = set()
    for agent in agents:
        capability = getattr(agent, "capability", None)
        for scope in getattr(capability, "access_scopes", None) or []:
            if isinstance(scope, str) and scope.startswith(SCOPE_PREFIX):
                scopes.add(scope)
    return scopes


async def enrich_mcp_data(
    task_context: dict[str, Any],
    needed_scopes: set[str],
    tenant_id: str,
    mcp_client: MCPClient,
    mcp_registry: MCPServerRegistry,
) -> dict[str, Any]:
    """Fetch MCP tool data for each needed mcp:* scope into task_context.

    For each scope: look up the tenant's registered server, resolve its
    credential from the environment, call its default tool, then sanitize
    and wrap the response before storing it under the scope key.

    Non-fatal by construction: a missing server, unresolved credential,
    or failed call logs a warning and moves on.
    """
    mcp_scopes = {s for s in needed_scopes if s.startswith(SCOPE_PREFIX)}
    if not mcp_scopes:
        return task_context

    for scope_key in sorted(mcp_scopes):
        config = mcp_registry.get_by_scope(scope_key, tenant_id)
        if config is None:
            logger.info(
                "[MCPEnrichment] No server registered for scope %s tenant %s",
                scope_key, tenant_id,
            )
            continue

        if not config.default_tool:
            logger.warning(
                "[MCPEnrichment] No default_tool configured for %s", config.name
            )
            continue

        auth_token = config.resolve_credential()
        if config.auth_type != "none" and not auth_token:
            logger.warning(
                "[MCPEnrichment] Credential not resolved for %s (env var: %s)",
                config.name, config.credential_env_var,
            )
            continue

        try:
            result = await mcp_client.call_tool(
                server_url=config.server_url,
                tool_name=config.default_tool,
                arguments=dict(config.default_arguments),
                tenant_id=tenant_id,
                auth_token=auth_token,
                timeout=config.timeout,
            )
        except Exception as exc:
            logger.warning(
                "[MCPEnrichment] MCP call failed for %s: %s",
                scope_key, type(exc).__name__,
            )
            continue

        if result.is_error:
            logger.warning(
                "[MCPEnrichment] MCP call failed for %s: %s",
                scope_key, result.error_message,
            )
            continue

        # Sanitize (truncate) FIRST, then scan: only content that can
        # actually reach a prompt is worth the Layer 2 decoding cost,
        # and a hostile server returning tens of MB must not buy CPU
        # time for bytes that get truncated anyway. The advanced scan
        # (Layer-2 imports stay lazy inside detect_injection_attempt)
        # runs off the event loop -- Unicode normalization plus decode
        # passes over 50k chars are real work. Log-only: the sanitized,
        # wrapped content proceeds regardless of findings.
        sanitized = sanitize_for_prompt(result.content, max_length=MAX_RESPONSE_CHARS)
        findings = await asyncio.to_thread(
            detect_injection_attempt, sanitized, advanced=True
        )
        if findings:
            logger.warning(
                "[MCPEnrichment] Injection patterns in %s response: %d finding(s)",
                scope_key, len(findings),
            )

        task_context[scope_key] = wrap_user_content(sanitized, label="MCP_DATA")
        logger.info(
            "[MCPEnrichment] Enriched %s from server '%s' (%.0fms)",
            scope_key, config.name, result.duration_ms,
        )

    return task_context
