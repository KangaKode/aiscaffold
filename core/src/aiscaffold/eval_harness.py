"""
Eval Harness - Run evaluations, record results, aggregate scores.

Provides the universal eval infrastructure. Project-specific graders
are built on top of GraderResult.

Usage:
    from aiscaffold import EvalHarness, GraderResult, SuiteResult

    result = GraderResult(eval_name="my_test", passed=True, score=0.95)
    suite = SuiteResult(suite_name="regression", results=[result])

    harness = EvalHarness()
    harness.save_results(suite)
    print(suite.format_summary())
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class GraderResult:
    """Result from a grader evaluation."""
    eval_name: str
    passed: bool
    score: float
    details: str = ""
    metrics: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"


@dataclass
class SuiteResult:
    """Result of running an eval suite."""
    suite_name: str
    results: list[GraderResult] = field(default_factory=list)
    run_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total > 0 else 0.0

    @property
    def avg_score(self) -> float:
        return sum(r.score for r in self.results) / len(self.results) if self.results else 0.0

    def format_summary(self) -> str:
        lines = [
            f"# Eval Suite: {self.suite_name}",
            f"**Run at:** {self.run_at}",
            f"**Pass rate:** {self.passed}/{self.total} ({self.pass_rate:.0%})",
            f"**Avg score:** {self.avg_score:.2f}",
            "",
            "| Eval | Status | Score | Details |",
            "|------|--------|-------|---------|",
        ]
        for r in self.results:
            lines.append(f"| {r.eval_name} | {r.status} | {r.score:.2f} | {r.details} |")
        return "\n".join(lines)


class EvalHarness:
    """Runs eval suites and manages results."""

    def __init__(self, results_dir: Path | str | None = None):
        self.results_dir = Path(results_dir) if results_dir else Path("evals/results")
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def save_results(self, suite_result: SuiteResult) -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = self.results_dir / f"{suite_result.suite_name}_{ts}.json"
        data = {
            "suite_name": suite_result.suite_name,
            "run_at": suite_result.run_at,
            "summary": {
                "total": suite_result.total,
                "passed": suite_result.passed,
                "failed": suite_result.failed,
                "pass_rate": suite_result.pass_rate,
                "avg_score": suite_result.avg_score,
            },
            "results": [asdict(r) for r in suite_result.results],
        }
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info(f"[EvalHarness] Results saved to {fp}")
        return fp

    def load_latest_results(self, suite_name: str) -> SuiteResult | None:
        files = sorted(self.results_dir.glob(f"{suite_name}_*.json"), reverse=True)
        if not files:
            return None
        with open(files[0]) as f:
            data = json.load(f)
        results = [
            GraderResult(
                eval_name=r["eval_name"], passed=r["passed"], score=r["score"],
                details=r.get("details", ""), metrics=r.get("metrics", {}),
                timestamp=r.get("timestamp", ""),
            )
            for r in data.get("results", [])
        ]
        return SuiteResult(suite_name=data["suite_name"], results=results, run_at=data.get("run_at", ""))

    def compare_results(self, suite_name: str, current: SuiteResult) -> str:
        previous = self.load_latest_results(suite_name)
        if not previous:
            return "No previous results to compare."
        lines = [
            f"# Comparison: {suite_name}",
            f"| Metric | Previous | Current | Change |",
            f"|--------|----------|---------|--------|",
            f"| Pass rate | {previous.pass_rate:.0%} | {current.pass_rate:.0%} | {(current.pass_rate - previous.pass_rate):+.0%} |",
            f"| Avg score | {previous.avg_score:.2f} | {current.avg_score:.2f} | {(current.avg_score - previous.avg_score):+.2f} |",
        ]
        if current.pass_rate < previous.pass_rate:
            lines.append("\n**WARNING: Pass rate regression detected!**")
        return "\n".join(lines)
