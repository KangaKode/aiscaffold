"""
Code-Based Graders -- deterministic evaluation with exact criteria.

Use for: pass/fail checks, string matching, schema validation, threshold checks.
Fast, cheap, reproducible. No LLM needed.

Reference: docs/REFERENCES.md (Anthropic Evals guide)
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CodeGraderResult:
    """Result from code-based grading."""

    eval_name: str
    passed: bool
    checks_passed: int = 0
    checks_total: int = 0
    failures: list[str] = field(default_factory=list)


class CodeGrader:
    """Deterministic grader that runs a list of check functions.

    Usage:
        grader = CodeGrader("round_table_consensus")
        grader.add_check("has_analyses", lambda r: len(r.analyses) > 0)
        grader.add_check("consensus_reached", lambda r: r.consensus_reached)
        result = grader.grade(round_table_result)
    """

    def __init__(self, eval_name: str):
        self.eval_name = eval_name
        self._checks: list[tuple[str, Callable]] = []

    def add_check(self, name: str, check_fn: Callable[[Any], bool]) -> "CodeGrader":
        """Add a named check function. Returns self for chaining."""
        self._checks.append((name, check_fn))
        return self

    def grade(self, output: Any) -> CodeGraderResult:
        """Run all checks against the output."""
        failures = []
        passed_count = 0

        for name, check_fn in self._checks:
            try:
                if check_fn(output):
                    passed_count += 1
                else:
                    failures.append(f"FAIL: {name}")
            except Exception as e:
                failures.append(f"ERROR: {name} -- {e}")

        return CodeGraderResult(
            eval_name=self.eval_name,
            passed=len(failures) == 0,
            checks_passed=passed_count,
            checks_total=len(self._checks),
            failures=failures,
        )
