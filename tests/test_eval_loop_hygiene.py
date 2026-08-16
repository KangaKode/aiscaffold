"""Contract tests for Medium eval-loop hygiene (factory-floor recipe + regression).

Pins: error-analysis recipe, one graduated regression example, honest docs,
opt-in Makefile (no default CI workflow edits), ISA claim-closure cross-link.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "template" / "{{project_slug}}"
EVALS = TEMPLATE / "evals"
MAKEFILE = TEMPLATE / "Makefile.jinja"
PYPROJECT = TEMPLATE / "pyproject.toml.jinja"
EVALS_README = EVALS / "README.md"
ERROR_RECIPE_MD = EVALS / "ERROR_ANALYSIS_RECIPE.md"
ERROR_ANALYSIS_PY = EVALS / "error_analysis.py"
ERROR_FIXTURE = EVALS / "fixtures" / "error_analysis_example.json"
TASK_RECIPE_TEST = EVALS / "tasks" / "test_error_analysis_recipe.py"
REGRESSION_TEST = EVALS / "regression" / "test_isa_open_claim_regression.py.jinja"
ROOT_EVAL_SCALING = REPO_ROOT / "docs" / "EVAL_SCALING_GUIDE.md"
TMPL_EVAL_SCALING = TEMPLATE / "docs" / "EVAL_SCALING_GUIDE.md"
FEATURES = REPO_ROOT / "docs" / "FEATURES.md"
GOVERNANCE = TEMPLATE / "docs" / "GOVERNANCE.md"
CI_WORKFLOW = TEMPLATE / ".github" / "workflows" / "ci.yml.jinja"


class TestEvalLoopHygieneDocs(unittest.TestCase):
    def test_features_receipt_gate_is_shipped_not_follow_up(self) -> None:
        text = FEATURES.read_text(encoding="utf-8")
        self.assertNotIn(
            "follow-up High-tier PR (`feat/pre-commit-review-gate`)",
            text,
            "FEATURES still describes the receipt gate as an unshipped follow-up",
        )
        doctor = next(
            line
            for line in text.splitlines()
            if line.lstrip().startswith("- **Capability Doctor**")
        )
        self.assertRegex(
            doctor,
            r"hooks-install|review.receipt|Bugbot",
            "Capability Doctor bullet should mention shipped receipt/hooks gating",
        )

    def test_evals_readme_does_not_claim_preference_graduation_is_eval(self) -> None:
        text = EVALS_README.read_text(encoding="utf-8")
        self.assertNotRegex(
            text,
            r"learning/graduation\.py.*implements this pattern",
            "evals/README must not claim preference graduation.py is eval graduation",
        )
        self.assertIn("preference graduation", text.lower())
        self.assertIn("not", text.lower())

    def test_eval_scaling_does_not_claim_default_ci_blocks_full_regression(self) -> None:
        for path in (ROOT_EVAL_SCALING, TMPL_EVAL_SCALING):
            text = path.read_text(encoding="utf-8")
            # Forbid the old absolute claim without an opt-in / Non-Claim nearby.
            self.assertFalse(
                re.search(
                    r"Regression tests run on every CI push",
                    text,
                )
                and "opt-in" not in text.lower()
                and "Non-Claim" not in text
                and "non-claim" not in text.lower(),
                f"{path}: still claims every-CI regression blocking without opt-in caveat",
            )
            self.assertNotRegex(
                text,
                r"(?m)^5\. Any regression failure blocks the PR\s*$",
                f"{path}: absolute 'blocks the PR' without opt-in framing",
            )

    def test_factory_floor_non_claim_present(self) -> None:
        readme = EVALS_README.read_text(encoding="utf-8")
        gov = GOVERNANCE.read_text(encoding="utf-8")
        blob = readme + "\n" + gov
        self.assertRegex(
            blob,
            r"(?i)factory.?floor|scaffold factory",
            "Need a Non-Claim that eval hygiene is factory-floor scoped",
        )
        self.assertRegex(
            blob,
            r"(?i)LLM-as-judge|model-based grader",
            "Need Non-Claim that LLM-as-judge is not default CI",
        )

    def test_isa_claim_closure_cross_link(self) -> None:
        recipe = ERROR_RECIPE_MD.read_text(encoding="utf-8")
        self.assertRegex(
            recipe,
            r"(?i)task isa|isa_closure|claim.?closure",
            "Recipe must cross-link Task ISA claim-closure",
        )


class TestEvalLoopHygieneArtifacts(unittest.TestCase):
    def test_recipe_module_and_fixture_exist(self) -> None:
        self.assertTrue(ERROR_ANALYSIS_PY.is_file(), ERROR_ANALYSIS_PY)
        self.assertTrue(ERROR_FIXTURE.is_file(), ERROR_FIXTURE)
        self.assertTrue(ERROR_RECIPE_MD.is_file(), ERROR_RECIPE_MD)
        self.assertTrue(TASK_RECIPE_TEST.is_file(), TASK_RECIPE_TEST)

    def test_regression_example_marked(self) -> None:
        self.assertTrue(REGRESSION_TEST.is_file(), REGRESSION_TEST)
        text = REGRESSION_TEST.read_text(encoding="utf-8")
        self.assertIn("@pytest.mark.regression", text)
        self.assertIn("evaluate_isa_closure", text)
        self.assertIn("all_required_closed", text)

    def test_regression_marker_registered(self) -> None:
        text = PYPROJECT.read_text(encoding="utf-8")
        self.assertRegex(
            text,
            r'regression:\s*Graduated eval suite',
            "pyproject must register pytest.mark.regression",
        )

    def test_makefile_eval_regression_noop_when_empty(self) -> None:
        text = MAKEFILE.read_text(encoding="utf-8")
        self.assertIn("eval-regression:", text)
        # Must not unconditionally pytest an empty tree (exit 5).
        self.assertRegex(
            text,
            r"eval-regression:.*\n(?:.*\n)*?.*(test_\*\.py|find |no regression|skipping)",
            "eval-regression must no-op cleanly when no regression tests exist",
        )

    def test_default_ci_workflow_unchanged_for_eval_regression(self) -> None:
        text = CI_WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("eval-regression", text)
        self.assertNotIn("evals/regression", text)


if __name__ == "__main__":
    unittest.main()
