"""
MCP tool-metadata screen -- Layer 1+2 on supplier tool strings.

On every list_tools() fetch, scan each tool's description plus a canonical
JSON dump of input_schema for injection patterns, and compare a sha256 of
the (size-capped) text to remembered hashes for rug-pull drift.

Default: detect-only (never filters the caller's list). When
``MCP_TOOL_METADATA_ENFORCEMENT_ENABLED`` is on and an outer screen
completes without raising, rebuild ``config.blocked_tools`` from
injection findings only (drift stays advisory). Filtering / refuse is
owned by ``mcp_client`` (READ gated by the same flag).

Flag persistence is injected via optional ``flag_hook`` (connectors must
not import learning/). Callers in api/ wire record_flag_hit.

Fail-open per tool: a raise while screening one tool is logged and that
tool is skipped; the caller's list is returned unmodified.

Keep this file under 230 lines.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..security.prompt_guard import detect_injection_attempt

logger = logging.getLogger(__name__)

FLAG_INJECTION = "mcp_tool_metadata_injection"
FLAG_DRIFT = "mcp_tool_description_drift"
ENFORCEMENT_ENV = "MCP_TOOL_METADATA_ENFORCEMENT_ENABLED"
BLOCK_REASON_INJECTION = "metadata_injection"
REFUSE_CODE = "tool_refused_metadata_injection"
MAX_SCAN_TEXT_CHARS = 8192
_TRUNCATE_SUFFIX = "\n[truncated]"
_MAX_PATTERNS = 10

FlagHook = Callable[[str, str, dict[str, Any]], None]


@dataclass(frozen=True)
class ToolScreenReport:
    """Transient advisory counts (never persisted on MCPServerConfig)."""

    tools_scanned: int = 0
    tools_flagged: int = 0
    tools_drifted: int = 0


def enforcement_enabled() -> bool:
    """True when MCP_TOOL_METADATA_ENFORCEMENT_ENABLED is truthy."""
    return os.environ.get(ENFORCEMENT_ENV, "").strip().lower() in (
        "true",
        "1",
        "yes",
    )


def tool_metadata_text(description: str, input_schema: Any) -> str:
    """Canonical scan/hash bytes source (schema key-order independent).

    Dicts get a canonical sort-keyed JSON dump. Anything else (list, str,
    None -- suppliers can and do return exotic shapes) is dumped as
    ``{"_non_object_schema": str(value)[:MAX_SCAN_TEXT_CHARS]}`` so
    injection patterns inside non-object schemas still enter the scan
    and drift hash instead of being silently discarded.
    """
    if isinstance(input_schema, dict):
        dumped = json.dumps(
            input_schema, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
    else:
        preview = "" if input_schema is None else str(input_schema)
        dumped = json.dumps(
            {"_non_object_schema": preview[:MAX_SCAN_TEXT_CHARS]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
    return f"{description or ''}\n{dumped}"


def cap_scan_text(text: str) -> str:
    """Truncate untrusted supplier text before scan and hash (same bytes)."""
    if len(text) <= MAX_SCAN_TEXT_CHARS:
        return text
    keep = MAX_SCAN_TEXT_CHARS - len(_TRUNCATE_SUFFIX)
    if keep < 1:
        return _TRUNCATE_SUFFIX[:MAX_SCAN_TEXT_CHARS]
    return text[:keep] + _TRUNCATE_SUFFIX


def metadata_sha256(capped_text: str) -> str:
    """Hex digest of UTF-8 capped metadata text."""
    return hashlib.sha256(capped_text.encode("utf-8")).hexdigest()


def screen_listed_tools(
    tools: list[Any],
    config: Any | None = None,
    flag_hook: FlagHook | None = None,
    report_out: dict[str, int] | None = None,
) -> ToolScreenReport:
    """Screen tools; optionally update hashes/blocks and invoke flag_hook.

    ``config`` must expose ``name`` and ``tool_desc_hashes`` when hash
    memory is desired; ``blocked_tools`` is rewritten only when
    enforcement is on and this call returns normally.
    ``flag_hook(flag_type, subject_id, detail)`` is called for findings
    when config.name is set (subject_id = ``server:tool``).
    """
    flagged = 0
    drifted = 0
    scanned = 0
    new_hashes: dict[str, str] = {}
    new_blocks: dict[str, str] = {}
    enforce = enforcement_enabled()
    prior = dict(getattr(config, "tool_desc_hashes", None) or {}) if config else {}
    server_name = getattr(config, "name", "") if config else ""

    prior_blocks = (
        dict(getattr(config, "blocked_tools", None) or {}) if config else {}
    )

    for tool in tools or []:
        name = ""
        try:
            name = getattr(tool, "name", None) or ""
            if not name:
                continue
            scanned += 1
            capped = cap_scan_text(
                tool_metadata_text(
                    getattr(tool, "description", "") or "",
                    getattr(tool, "input_schema", None),
                )
            )
            digest = metadata_sha256(capped)
            new_hashes[name] = digest

            findings = detect_injection_attempt(capped, advanced=True) or []
            if findings:
                flagged += 1
                logger.warning(
                    "[ToolScreen] Injection patterns in tool metadata "
                    "server=%s tool=%s findings=%d (detect-only)",
                    server_name or "<bare>",
                    name,
                    len(findings),
                )
                _emit(
                    flag_hook,
                    FLAG_INJECTION,
                    server_name,
                    name,
                    {"patterns": list(findings)[:_MAX_PATTERNS]},
                )
                if enforce:
                    new_blocks[name] = BLOCK_REASON_INJECTION

            if config is not None and name in prior and prior[name] != digest:
                drifted += 1
                logger.warning(
                    "[ToolScreen] Tool metadata drift server=%s tool=%s "
                    "(detect-only)",
                    server_name,
                    name,
                )
                _emit(
                    flag_hook,
                    FLAG_DRIFT,
                    server_name,
                    name,
                    {"previous_sha256": prior[name][:16], "sha256": digest[:16]},
                )
        except Exception:
            logger.warning(
                "[ToolScreen] Per-tool screen failed (fail-open)",
                exc_info=True,
            )
            # Fail-open on list, but do not clear a prior injection block
            # for a tool that was never re-verified clean this screen.
            if (
                enforce
                and name
                and prior_blocks.get(name) == BLOCK_REASON_INJECTION
            ):
                new_blocks[name] = BLOCK_REASON_INJECTION
            continue

    if config is not None:
        config.tool_desc_hashes = new_hashes
        # WRITE blocked_tools only when enforce ON + outer screen OK.
        # Enforce OFF never clears (stale map inert because READ is gated).
        if enforce:
            config.blocked_tools = new_blocks

    report = ToolScreenReport(
        tools_scanned=scanned, tools_flagged=flagged, tools_drifted=drifted
    )
    if report_out is not None:
        report_out["tools_scanned"] = report.tools_scanned
        report_out["tools_flagged"] = report.tools_flagged
        report_out["tools_drifted"] = report.tools_drifted
        if enforce:
            report_out["tools_refused"] = len(new_blocks)
    return report


def _emit(
    flag_hook: FlagHook | None,
    flag_type: str,
    server_name: str,
    tool_name: str,
    detail: dict[str, Any],
) -> None:
    if flag_hook is None or not server_name:
        return
    try:
        flag_hook(flag_type, f"{server_name}:{tool_name}", detail)
    except Exception:
        logger.warning("[ToolScreen] flag_hook failed (non-fatal)", exc_info=True)
