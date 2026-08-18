"""Recipe tests: fixture → FailureMode → suggested evals/tasks stub."""

from pathlib import Path

import pytest

from evals.error_analysis import FailureMode, analyze_failure, analyze_failure_file

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "error_analysis_example.json"


class TestErrorAnalysisRecipe:
    def test_fixture_yields_structured_failure_mode(self):
        mode = analyze_failure_file(FIXTURE)
        assert isinstance(mode, FailureMode)
        assert mode.failure_id == "isa_required_claim_open"
        assert "Task ISA" in mode.given or "isa" in mode.given.lower()
        assert mode.expected
        assert mode.actual
        assert mode.suggested_task_stub == (
            "evals/tasks/test_isa_required_claim_open.py"
        )

    def test_missing_keys_raise(self):
        with pytest.raises(ValueError, match="missing keys"):
            analyze_failure({"failure_id": "x", "given": "g"})

    def test_promotion_path_documented_for_fixture(self):
        """Until fixed, the fixture maps to a task stub path (promotion contract)."""
        mode = analyze_failure_file(FIXTURE)
        assert mode.suggested_task_stub.startswith("evals/tasks/test_")
        assert mode.suggested_task_stub.endswith(".py")
        # Graduated twin lives under regression/ after generate (see ISA open-claim).
        regression = Path(__file__).resolve().parents[1] / "regression"
        twins = list(regression.glob("test_isa_open_claim_regression.py*"))
        assert twins, "expected graduated ISA open-claim regression twin"
