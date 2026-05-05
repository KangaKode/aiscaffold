"""
System Evals (3 tasks) -- cost tracking, protocol compliance, human grader example.

Mix of CODE-BASED graders and a HUMAN grader example.
"""

import os
import pytest

from {{ project_slug }}.llm.client import TokenUsage, CacheablePrompt
from evals.graders.code_grader import CodeGrader


class TestCostTracking:
    """Eval: Does token tracking accurately accumulate costs?"""

    def test_token_usage_accumulates(self):
        grader = CodeGrader("cost_tracking")
        usage = TokenUsage(input_tokens=100, output_tokens=50)
        grader.add_check("total_correct", lambda u: u.total_tokens == 150)
        grader.add_check("input_tracked", lambda u: u.input_tokens == 100)
        grader.add_check("output_tracked", lambda u: u.output_tokens == 50)
        result = grader.grade(usage)
        assert result.passed

    def test_cacheable_prompt_separates_stable_from_dynamic(self):
        """Verify prompt caching architecture: system is stable, user varies."""
        prompt = CacheablePrompt(
            system="You are a helpful assistant",
            context="Project context here",
            user_message="Analyze this specific input",
        )
        grader = CodeGrader("prompt_caching_structure")
        grader.add_check("system_present", lambda p: bool(p.system))
        grader.add_check("user_present", lambda p: bool(p.user_message))
        grader.add_check("separates_stable_prefix", lambda p: p.system != p.user_message)
        grader.add_check("total_length_correct", lambda p: p.total_length > 0)
        result = grader.grade(prompt)
        assert result.passed


class TestAgentProtocolCompliance:
    """Eval: Do core agents comply with the AgentProtocol?"""

    def test_core_agents_implement_protocol(self):
        """All core agents must have name, domain, analyze, challenge, vote."""
        from {{ project_slug }}.agents.core import get_core_agents
        from {{ project_slug }}.orchestration.round_table import AgentProtocol

        agents = get_core_agents(llm_client=None)
        for agent in agents:
            assert isinstance(agent, AgentProtocol), f"{agent.name} doesn't implement AgentProtocol"

    def test_core_agents_have_unique_names(self):
        from {{ project_slug }}.agents.core import get_core_agents
        agents = get_core_agents(llm_client=None)
        names = [a.name for a in agents]
        assert len(names) == len(set(names)), f"Duplicate names: {names}"


class TestHumanGraderExample:
    """Eval: Demonstrates the HUMAN grader workflow.

    This creates a review file for manual inspection. It's not auto-graded.
    Run with: EVAL_USE_REAL_LLM=1 make eval
    """

    REAL_LLM = os.getenv("EVAL_USE_REAL_LLM", "0") == "1"

    @pytest.mark.skipif(not REAL_LLM, reason="Human grader demo requires EVAL_USE_REAL_LLM=1")
    def test_submit_synthesis_for_human_review(self, tmp_path):
        """Submit a synthesis output for manual quality review."""
        from evals.graders.human_grader import HumanGrader

        grader = HumanGrader("synthesis_human_review", review_dir=tmp_path)
        filepath = grader.submit_for_review(
            input_text="Analyze the authentication module for OWASP Top 10 vulnerabilities",
            output_text=(
                "Recommendation: Fix SQL injection on line 42 using parameterized queries. "
                "Add rate limiting to login endpoint. Enable HSTS headers."
            ),
            rubric=(
                "1. Is the recommendation specific and actionable?\n"
                "2. Does it address real OWASP categories?\n"
                "3. Would you trust this recommendation in a code review?"
            ),
        )
        assert filepath.exists()
        assert "synthesis_human_review" in filepath.name
