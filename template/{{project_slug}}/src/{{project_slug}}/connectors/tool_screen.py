"""
MCP tool-metadata screen -- detect-only Layer 1+2 on supplier tool strings.

On every list_tools() fetch, scan each tool's description plus a canonical
JSON dump of input_schema for injection patterns, and compare a sha256 of
the (size-capped) text to remembered hashes for rug-pull drift. Never
mutates or filters the tool list.

Flag persistence is injected via optional ``flag_hook`` (connectors must
not import learning/). Callers in api/ wire record_flag_hit.

Fail-open per tool: a raise while screening one tool is logged and that
tool is skipped; the caller's list is returned unmodified.

Keep this file under 200 lines.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..security.prompt_guard import detect_injection_attempt

logger = logging.getLogger(__name__)

FLAG_INJECTION = "mcp_tool_metadata_injection"
FLAG_DRIFT = "mcp_tool_description_drift"
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
    """Screen tools detect-only; optionally update hashes and invoke flag_hook.

    ``config`` must expose ``name`` and ``tool_desc_hashes`` when hash memory
    is desired. ``flag_hook(flag_type, subject_id, detail)`` is called for
    findings when config.name is set (subject_id = ``server:tool``).
    """
    flagged = 0
    drifted = 0
    scanned = 0
    new_hashes: dict[str, str] = {}
    prior = dict(getattr(config, "tool_desc_hashes", None) or {}) if config else {}
    server_name = getattr(config, "name", "") if config else ""

    for tool in tools or []:
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
            continue

    if config is not None:
        config.tool_desc_hashes = new_hashes

    report = ToolScreenReport(
        tools_scanned=scanned, tools_flagged=flagged, tools_drifted=drifted
    )
    if report_out is not None:
        report_out["tools_scanned"] = report.tools_scanned
        report_out["tools_flagged"] = report.tools_flagged
        report_out["tools_drifted"] = report.tools_drifted
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
