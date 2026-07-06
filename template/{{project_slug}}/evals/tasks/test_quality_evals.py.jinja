"""
Quality Evals (3 tasks) -- evidence levels, citation format, output structure.

Mix of CODE-BASED and MODEL-BASED grader examples.
Code-based evals run without LLM. Model-based show the pattern for when you need one.
"""

import os
import pytest

from {{ project_slug }}.enforcement.evidence_levels import EvidenceLevelEnforcer
from evals.graders.code_grader import CodeGrader


class TestEvidenceLevelFormat:
    """Eval: Are evidence level tags properly formatted? (Code-based grader)"""

    def test_verified_requires_source_and_reference(self):
        enforcer = EvidenceLevelEnforcer()
        result = enforcer.check("[VERIFIED: just_a_source] No reference provided")
        assert any("verified" in v.rule for v in result.violations)

    def test_corroborated_requires_two_sources(self):
        enforcer = EvidenceLevelEnforcer()
        result = enforcer.check("[CORROBORATED: only_one] Single source claim")
        assert any("corroborated" in v.rule for v in result.violations)

    def test_valid_evidence_tags_accepted(self):
        enforcer = EvidenceLevelEnforcer()
        text = (
            "[VERIFIED: logs:row_42] Found the entry. "
            "[CORROBORATED: logs + alerts] Both sources agree. "
            "[INDICATED: network_data] Pattern suggests lateral movement. "
            "[POSSIBLE] VPN logs would confirm this."
        )
        result = enforcer.check(text)
        assert result.outcome == "accepted"


class TestAgentOutputStructure:
    """Eval: Does agent output meet structural requirements? (Code-based grader)"""

    def test_analysis_has_required_fields(self):
        """Every AgentAnalysis must have agent_name, domain, and observations."""
        from {{ project_slug }}.orchestration.round_table import AgentAnalysis

        grader = CodeGrader("analysis_structure")
        grader.add_check("has_agent_name", lambda a: bool(a.agent_name))
        grader.add_check("has_domain", lambda a: bool(a.domain))
        grader.add_check("has_observations", lambda a: isinstance(a.observations, list))

        analysis = AgentAnalysis(
            agent_name="test_agent",
            domain="testing",
            observations=[{"finding": "test", "evidence": "test", "severity": "info"}],
        )
        result = grader.grade(analysis)
        assert result.passed
        assert result.checks_passed == 3


class TestModelBasedQualityEval:
    """Eval: Is the synthesis recommendation actionable? (Model-based grader example)

    This demonstrates the MODEL-BASED grader pattern. It requires a real LLM
    to run. Skip in CI; use for periodic quality checks.
    """

    REAL_LLM = os.getenv("EVAL_USE_REAL_LLM", "0") == "1"

    @pytest.mark.skipif(not REAL_LLM, reason="Requires EVAL_USE_REAL_LLM=1")
    @pytest.mark.asyncio
    async def test_synthesis_is_actionable(self):
        """Use LLM-as-judge to evaluate synthesis quality."""
        from evals.graders.model_graders import ModelGraderConfig, grade_with_model
        from {{ project_slug }}.llm import create_client

        llm = create_client()
        config = ModelGraderConfig(
            eval_name="synthesis_actionable",
            rubric=(
                "Score the recommendation on actionability:\n"
                "- 1.0: Specific next steps with clear owners\n"
                "- 0.7: General direction but actionable\n"
                "- 0.3: Vague advice without specifics\n"
                "- 0.0: No actionable content"
            ),
            pass_threshold=0.7,
        )
        result = await grade_with_model(
            llm, config,
            input_text="Review authentication module for security vulnerabilities",
            output_text="Fix the SQL injection on line 42 by using parameterized queries. Add rate limiting to the login endpoint.",
        )
        assert result.passed
