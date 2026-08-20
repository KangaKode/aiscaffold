"""Contract tests for Medium governed self-learning Phase 1.

Pins LEARN/retrieval docs, ISA reflection metrics path, graded feedback
intake, and Non-Claim that learnings never auto-edit skills/hooks/prompts.
No learning/tables.py schema changes in this PR.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "template" / "{{project_slug}}"
FEATURES = REPO_ROOT / "docs" / "FEATURES.md"
GOVERNANCE = TEMPLATE / "docs" / "GOVERNANCE.md"
REFLECTOR = TEMPLATE / "src" / "{{project_slug}}" / "learning" / "reflector.py"
GRADED = TEMPLATE / "src" / "{{project_slug}}" / "learning" / "graded_intake.py"
FEEDBACK = TEMPLATE / "src" / "{{project_slug}}" / "api" / "routes" / "feedback.py"
TABLES = TEMPLATE / "src" / "{{project_slug}}" / "learning" / "tables.py"
KNOWLEDGE = TEMPLATE / "src" / "{{project_slug}}" / "learning" / "knowledge_context.py"


class TestGovernedSelfLearningDocs(unittest.TestCase):
    def test_learn_contract_documented(self) -> None:
        blob = FEATURES.read_text(encoding="utf-8") + "\n" + GOVERNANCE.read_text(
            encoding="utf-8"
        )
        self.assertRegex(blob, r"LEARN contract|### LEARN contract", re.I)
        self.assertRegex(blob, r"four-eyes|check-in", re.I)
        self.assertRegex(blob, r"build_knowledge_context")

    def test_retrieval_contract_cites_tiers(self) -> None:
        blob = FEATURES.read_text(encoding="utf-8") + "\n" + GOVERNANCE.read_text(
            encoding="utf-8"
        )
        self.assertRegex(blob, r"/resolve")
        self.assertRegex(blob, r"chat")
        self.assertRegex(blob, r"round-table|round_table")

    def test_non_claim_no_auto_edit_skills_hooks_prompts(self) -> None:
        gov = GOVERNANCE.read_text(encoding="utf-8")
        self.assertRegex(
            gov,
            r"(?i)does not auto-?(edit|modify|rewrite).{0,80}(skills|hooks|prompts)",
            "GOVERNANCE Non-Claim must state learnings do not auto-edit "
            "skills/hooks/prompts",
        )


class TestGovernedSelfLearningCode(unittest.TestCase):
    def test_reflector_merges_isa_closure_into_quality_metrics(self) -> None:
        text = REFLECTOR.read_text(encoding="utf-8")
        self.assertIn("isa_closure", text)
        self.assertIn("quality_metrics", text)
        self.assertIn("_isa_closure_summary", text)

    def test_graded_intake_module_and_feedback_wiring(self) -> None:
        self.assertTrue(GRADED.is_file(), GRADED)
        graded = GRADED.read_text(encoding="utf-8")
        self.assertIn("grade_feedback_content", graded)
        self.assertIn("detect_injection_attempt", graded)
        fb = FEEDBACK.read_text(encoding="utf-8")
        self.assertIn("grade_feedback_content", fb)
        self.assertRegex(
            fb,
            r"grade_feedback_content[\s\S]{0,800}update_from_signal",
            "feedback route must grade before deciding on trust update",
        )

    def test_no_tables_py_schema_change_in_phase1_marker(self) -> None:
        """Sanity: knowledge_context still the retrieval surface; tables exist."""
        self.assertTrue(TABLES.is_file())
        self.assertTrue(KNOWLEDGE.is_file())
        kc = KNOWLEDGE.read_text(encoding="utf-8")
        self.assertIn("Tier 1", kc)
        self.assertIn("Tier 2", kc)
        self.assertIn("Tier 3", kc)


if __name__ == "__main__":
    unittest.main()
