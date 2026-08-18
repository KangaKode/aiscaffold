"""Error-analysis helpers for factory-floor eval hygiene.

Turn an audit / metrics / phase-artifact snippet (or a synthetic fixture) into a
structured failure-mode record, then into a suggested evals/tasks stub path.

This module is consume-only: it does not invent instrumentation and does not
call LLMs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FailureMode:
    """Structured given / expected / actual failure record."""

    failure_id: str
    given: str
    expected: str
    actual: str
    source: str = ""
    suggested_task_stub: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def analyze_failure(record: dict[str, Any]) -> FailureMode:
    """Build a FailureMode from a fixture or audit-snippet dict.

    Required keys: failure_id, given, expected, actual.
    Optional: source (path or artifact name).
    """
    missing = [k for k in ("failure_id", "given", "expected", "actual") if k not in record]
    if missing:
        raise ValueError(f"failure record missing keys: {', '.join(missing)}")

    failure_id = str(record["failure_id"]).strip()
    if not failure_id:
        raise ValueError("failure_id must be non-empty")

    stub = f"evals/tasks/test_{_safe_stem(failure_id)}.py"
    return FailureMode(
        failure_id=failure_id,
        given=str(record["given"]),
        expected=str(record["expected"]),
        actual=str(record["actual"]),
        source=str(record.get("source") or ""),
        suggested_task_stub=stub,
    )


def analyze_failure_file(path: Path | str) -> FailureMode:
    """Load JSON/dict-like fixture from path and analyze."""
    import json

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("failure fixture must be a JSON object")
    return analyze_failure(data)


def _safe_stem(failure_id: str) -> str:
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in failure_id)
    return stem.strip("_") or "unnamed_failure"
