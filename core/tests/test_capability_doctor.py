"""Tests for Capability Doctor probes and aiscaffold doctor flags."""

from pathlib import Path

from typer.testing import CliRunner

from aiscaffold import cli
from aiscaffold.capability_doctor import (
    CapabilityRow,
    has_broken,
    probe_capabilities,
    rows_to_json,
)


def _minimal_project(root: Path) -> None:
    (root / "docs").mkdir()
    (root / "tests").mkdir()
    (root / "CLAUDE.md").write_text("#")
    (root / "docs" / "ARCHITECTURE.md").write_text("#")
    (root / "tests" / "test_architecture.py").write_text("def test_ok():\n    assert True\n")
    (root / ".gitignore").write_text("")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")


def test_clean_fixture_structure_live(tmp_path):
    _minimal_project(tmp_path)
    rows = {r.id: r for r in probe_capabilities(tmp_path)}
    assert rows["project_structure"].state == "live"
    assert not has_broken([rows["project_structure"]])


def test_missing_required_file_structure_broken(tmp_path):
    _minimal_project(tmp_path)
    (tmp_path / "CLAUDE.md").unlink()
    rows = {r.id: r for r in probe_capabilities(tmp_path)}
    assert rows["project_structure"].state == "broken"


def test_llm_unset_is_unconfigured(tmp_path, monkeypatch):
    _minimal_project(tmp_path)
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    rows = {r.id: r for r in probe_capabilities(tmp_path)}
    assert rows["llm_provider"].state == "unconfigured"
    assert rows["llm_provider"].fix_command
    assert "sk-" not in rows["llm_provider"].detail


def test_enforcement_default_declined(tmp_path, monkeypatch):
    _minimal_project(tmp_path)
    for f in (
        "SENTINEL_ENFORCEMENT_ENABLED",
        "EXTRACTION_GUARD_ENFORCE",
        "RUNTIME_CANARY_ENFORCEMENT_ENABLED",
        "MCP_TOOL_METADATA_ENFORCEMENT_ENABLED",
    ):
        monkeypatch.delenv(f, raising=False)
    rows = {r.id: r for r in probe_capabilities(tmp_path)}
    assert rows["enforcement_pipeline"].state == "declined"


def test_json_schema_stable(tmp_path):
    _minimal_project(tmp_path)
    payload = rows_to_json(probe_capabilities(tmp_path))
    assert '"capabilities"' in payload
    assert '"id"' in payload
    assert '"state"' in payload
    assert '"detail"' in payload


def test_strict_capabilities_exits_nonzero(tmp_path, monkeypatch):
    _minimal_project(tmp_path)
    (tmp_path / "CLAUDE.md").unlink()
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["doctor", str(tmp_path), "--strict-capabilities"],
    )
    assert result.exit_code == 1


def test_doctor_epilogue_never_pass_with_broken_caps():
    assert cli._doctor_epilogue(warning_count=0, broken_capability_count=1).startswith(
        "WARN:"
    )
    assert "capability row(s) broken" in cli._doctor_epilogue(
        warning_count=0, broken_capability_count=2
    )
    assert cli._doctor_epilogue(warning_count=0, broken_capability_count=0) == "PASS"
    assert cli._doctor_epilogue(warning_count=3, broken_capability_count=0).startswith(
        "WARN:"
    )


def test_has_broken_helper():
    assert has_broken([CapabilityRow("x", "X", "broken", "nope", None)])
    assert not has_broken([CapabilityRow("x", "X", "live", "ok", None)])
