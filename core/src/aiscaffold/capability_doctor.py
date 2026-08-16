"""Capability Doctor -- read-only health matrix for scaffold/project ops.

States: live | broken | declined | stale | unconfigured.
Never prints secret values. No network probes in Phase 1.

Keep under 280 lines.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

CapabilityState = Literal["live", "broken", "declined", "stale", "unconfigured"]


@dataclass(frozen=True)
class CapabilityRow:
    id: str
    label: str
    state: CapabilityState
    detail: str
    fix_command: str | None = None


def rows_to_json(rows: list[CapabilityRow]) -> str:
    return json.dumps({"capabilities": [asdict(r) for r in rows]}, indent=2)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _probe_structure(root: Path) -> CapabilityRow:
    required = [
        "CLAUDE.md",
        "docs/ARCHITECTURE.md",
        "tests/test_architecture.py",
        ".gitignore",
        "pyproject.toml",
    ]
    missing = [f for f in required if not (root / f).exists()]
    if missing:
        return CapabilityRow(
            id="project_structure",
            label="Project structure",
            state="broken",
            detail=f"Missing: {', '.join(missing)}",
            fix_command="aiscaffold doctor .",
        )
    return CapabilityRow(
        id="project_structure",
        label="Project structure",
        state="live",
        detail="Required scaffold files present",
        fix_command=None,
    )


def _probe_llm(root: Path) -> CapabilityRow:
    # Presence only — never echo values.
    keys = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "LLM_API_KEY")
    if any(os.environ.get(k) for k in keys):
        return CapabilityRow(
            id="llm_provider",
            label="LLM provider",
            state="live",
            detail="Provider API key env var is set (value redacted)",
            fix_command=None,
        )
    cfg = root / ".env"
    if cfg.is_file():
        text = cfg.read_text(encoding="utf-8", errors="replace")
        if any(k in text for k in keys):
            return CapabilityRow(
                id="llm_provider",
                label="LLM provider",
                state="unconfigured",
                detail=".env mentions a provider key name; process env may be unset",
                fix_command="export ANTHROPIC_API_KEY=...  # or matching provider key",
            )
    return CapabilityRow(
        id="llm_provider",
        label="LLM provider",
        state="unconfigured",
        detail="No provider API key detected in process env",
        fix_command="export ANTHROPIC_API_KEY=...  # or OPENAI_API_KEY / GOOGLE_API_KEY",
    )


def _probe_learning(root: Path) -> CapabilityRow:
    db = root / "data" / "learning.db"
    if db.is_file():
        return CapabilityRow(
            id="learning_store",
            label="Learning store",
            state="live",
            detail="data/learning.db present",
            fix_command=None,
        )
    if (root / "src").is_dir():
        return CapabilityRow(
            id="learning_store",
            label="Learning store",
            state="unconfigured",
            detail="No data/learning.db yet (created on first store use)",
            fix_command="python -c \"from pathlib import Path; Path('data').mkdir(exist_ok=True)\"",
        )
    return CapabilityRow(
        id="learning_store",
        label="Learning store",
        state="unconfigured",
        detail="Learning DB not present",
        fix_command=None,
    )


def _probe_enforcement(root: Path) -> CapabilityRow:
    enforce_flags = (
        "SENTINEL_ENFORCEMENT_ENABLED",
        "EXTRACTION_GUARD_ENFORCE",
        "RUNTIME_CANARY_ENFORCEMENT_ENABLED",
        "MCP_TOOL_METADATA_ENFORCEMENT_ENABLED",
    )
    if any(_env_truthy(f) for f in enforce_flags):
        return CapabilityRow(
            id="enforcement_pipeline",
            label="Enforcement pipeline",
            state="live",
            detail="At least one enforce-* env flag is enabled",
            fix_command=None,
        )
    return CapabilityRow(
        id="enforcement_pipeline",
        label="Enforcement pipeline",
        state="declined",
        detail="Enforce flags default off (detect-only posture)",
        fix_command="export SENTINEL_ENFORCEMENT_ENABLED=true  # opt-in only",
    )


def _probe_mcp(root: Path) -> CapabilityRow:
    answers = root / ".copier-answers.yml"
    if answers.is_file():
        text = answers.read_text(encoding="utf-8", errors="replace")
        if "include_mcp: false" in text or "include_mcp: False" in text:
            return CapabilityRow(
                id="mcp_client",
                label="MCP client",
                state="declined",
                detail="include_mcp=false in .copier-answers.yml",
                fix_command=None,
            )
    mcp_paths = list(root.glob("**/connectors/mcp*.py")) + list(
        root.glob("**/mcp*.py")
    )
    if mcp_paths:
        return CapabilityRow(
            id="mcp_client",
            label="MCP client",
            state="live",
            detail="MCP module files present",
            fix_command=None,
        )
    return CapabilityRow(
        id="mcp_client",
        label="MCP client",
        state="unconfigured",
        detail="No MCP module detected",
        fix_command=None,
    )


def _probe_api(root: Path) -> CapabilityRow:
    answers = root / ".copier-answers.yml"
    if answers.is_file():
        text = answers.read_text(encoding="utf-8", errors="replace")
        if "include_api_gateway: false" in text:
            return CapabilityRow(
                id="api_gateway",
                label="API gateway",
                state="declined",
                detail="include_api_gateway=false",
                fix_command=None,
            )
    if (root / "src").is_dir() and list(root.glob("**/api/app.py")):
        return CapabilityRow(
            id="api_gateway",
            label="API gateway",
            state="live",
            detail="api/app.py present",
            fix_command=None,
        )
    return CapabilityRow(
        id="api_gateway",
        label="API gateway",
        state="unconfigured",
        detail="API app not found",
        fix_command=None,
    )


def _probe_budget(root: Path) -> CapabilityRow:
    if list(root.glob("**/llm/budget_manager.py")):
        return CapabilityRow(
            id="budget_manager",
            label="Budget manager",
            state="live",
            detail="budget_manager.py present",
            fix_command=None,
        )
    return CapabilityRow(
        id="budget_manager",
        label="Budget manager",
        state="unconfigured",
        detail="budget_manager.py not found",
        fix_command=None,
    )


def _probe_remote(root: Path) -> CapabilityRow:
    if list(root.glob("**/agents/remote.py")):
        return CapabilityRow(
            id="remote_agents",
            label="Remote agents",
            state="live",
            detail="agents/remote.py present (URL validation at register time)",
            fix_command=None,
        )
    return CapabilityRow(
        id="remote_agents",
        label="Remote agents",
        state="unconfigured",
        detail="remote agent module not found",
        fix_command=None,
    )


def probe_capabilities(root: Path | None = None) -> list[CapabilityRow]:
    """Run all Phase-1 capability probes against a project root."""
    root = (root or Path(".")).resolve()
    probes = (
        _probe_structure,
        _probe_llm,
        _probe_learning,
        _probe_enforcement,
        _probe_mcp,
        _probe_api,
        _probe_budget,
        _probe_remote,
    )
    rows: list[CapabilityRow] = []
    for probe in probes:
        try:
            rows.append(probe(root))
        except Exception as exc:  # noqa: BLE001 -- row-level isolation
            rows.append(
                CapabilityRow(
                    id=getattr(probe, "__name__", "unknown").removeprefix("_probe_"),
                    label=getattr(probe, "__name__", "unknown"),
                    state="broken",
                    detail=f"{type(exc).__name__}: {exc}"[:200],
                    fix_command="aiscaffold doctor .",
                )
            )
    return rows


def has_broken(rows: list[CapabilityRow]) -> bool:
    return any(r.state == "broken" for r in rows)


def report_as_dicts(rows: list[CapabilityRow]) -> list[dict[str, Any]]:
    return [asdict(r) for r in rows]
