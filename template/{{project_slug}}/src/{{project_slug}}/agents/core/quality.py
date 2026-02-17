"""
Quality Agent -- Completeness and requirement coverage checker.

Ensures nothing critical is overlooked. Tracks whether all requirements
and constraints are addressed, flags gaps in scope, and verifies the
synthesis covers the full problem space.

This is a core safety agent. It participates automatically unless
include_core_agents=False is set in RoundTableConfig.
"""

import json
import logging

from ...llm import CacheablePrompt
from ...orchestration.round_table import (
    AgentAnalysis,
    AgentChallenge,
    AgentVote,
    RoundTableTask,
    SynthesisResult,
)

logger = logging.getLogger(__name__)


class QualityAgent:
    """Checks completeness and requirement coverage across all agents.

    Phase 1 (Analysis): Maps requirements/constraints, identifies scope.
    Phase 2 (Challenge): Highlights gaps -- what did other agents miss?
    Phase 3 (Voting): Votes on whether the synthesis is complete.
    """

    def __init__(self, llm_client=None):
        self._llm = llm_client

    @property
    def name(self) -> str:
        return "quality"

    @property
    def domain(self) -> str:
        return "completeness and requirement coverage"

    def _system_prompt(self) -> str:
        return (
            "You are a Quality agent focused on completeness and coverage.\n\n"
            "Your role:\n"
            "- Extract all requirements and constraints from the task\n"
            "- Track which requirements each agent addressed\n"
            "- Flag requirements that NO agent addressed\n"
            "- Check for edge cases, boundary conditions, and error scenarios\n"
            "- Verify the synthesis covers the full scope\n\n"
            "Rules:\n"
            "- Be specific: name the exact requirement or constraint that's missing\n"
            "- Don't repeat what other agents said -- focus on what they DIDN'T say\n"
            "- Grade on coverage, not quality (that's the Skeptic's job)\n"
            "- Always return valid JSON\n"
        )

    async def analyze(self, task: RoundTableTask) -> AgentAnalysis:
        """Map requirements and identify what needs to be covered."""
        if not self._llm:
            return AgentAnalysis(
                agent_name=self.name,
                domain=self.domain,
                observations=[{
                    "finding": "Requirements not mapped (no LLM available)",
                    "evidence": "Quality agent requires LLM to extract requirements",
                    "severity": "warning",
                    "confidence": 0.5,
                }],
            )

        constraints_ctx = ""
        if task.constraints:
            constraints_ctx = f"\nExplicit constraints: {task.constraints}"

        prompt = CacheablePrompt(
            system=self._system_prompt(),
            user_message=(
                f"Extract all requirements, constraints, and success criteria "
                f"from this task:\n\n{task.content}{constraints_ctx}\n\n"
                f"Return JSON: {{\"observations\": [{{\"finding\": ..., "
                f"\"evidence\": ..., \"severity\": ..., \"confidence\": ...}}], "
                f"\"recommendations\": [...]}}"
            ),
        )
        response = await self._llm.call(prompt=prompt, role="quality_analysis")

        try:
            data = json.loads(response.content)
            return AgentAnalysis(
                agent_name=self.name,
                domain=self.domain,
                observations=data.get("observations", []),
                recommendations=data.get("recommendations", []),
            )
        except json.JSONDecodeError:
            return AgentAnalysis(
                agent_name=self.name,
                domain=self.domain,
                observations=[{
                    "finding": response.content[:500],
                    "evidence": "raw quality response",
                    "severity": "info",
                    "confidence": 0.5,
                }],
            )

    async def challenge(
        self, task: RoundTableTask, other_analyses: list[AgentAnalysis]
    ) -> AgentChallenge:
        """Identify gaps -- what did other agents miss?"""
        if not self._llm or not other_analyses:
            return AgentChallenge(agent_name=self.name)

        analyses_summary = json.dumps(
            [{"agent": a.agent_name, "findings": a.observations[:5]}
             for a in other_analyses if a.agent_name != self.name],
            indent=2, default=str,
        )

        prompt = CacheablePrompt(
            system=self._system_prompt(),
            context=f"Other agents' analyses:\n{analyses_summary}",
            user_message=(
                "Identify gaps in coverage across all agents' analyses.\n"
                "For each gap:\n"
                "- Name the requirement or scenario that was not addressed\n"
                "- Identify which agent should have covered it\n\n"
                "Return JSON: {\"challenges\": [{\"target_agent\": ..., "
                "\"finding_challenged\": ..., \"counter_evidence\": ...}], "
                "\"concessions\": [{\"target_agent\": ..., "
                "\"finding_accepted\": ..., \"reason\": ...}]}"
            ),
        )
        response = await self._llm.call(prompt=prompt, role="quality_challenge")

        try:
            data = json.loads(response.content)
            return AgentChallenge(
                agent_name=self.name,
                challenges=data.get("challenges", []),
                concessions=data.get("concessions", []),
            )
        except json.JSONDecodeError:
            return AgentChallenge(agent_name=self.name)

    async def vote(
        self, task: RoundTableTask, synthesis: SynthesisResult
    ) -> AgentVote:
        """Vote on whether the synthesis covers all requirements."""
        if not self._llm:
            return AgentVote(agent_name=self.name, approve=False,
                             dissent_reason="Cannot evaluate completeness without LLM")

        prompt = CacheablePrompt(
            system=self._system_prompt(),
            user_message=(
                f"Does this synthesis cover all requirements from the task?\n\n"
                f"Task: {task.content[:500]}\n"
                f"Recommendation: {synthesis.recommended_direction}\n"
                f"Key findings: {json.dumps(synthesis.key_findings[:5], default=str)}\n\n"
                f"Vote APPROVE if coverage is adequate.\n"
                f"Vote DISSENT if critical requirements are missing.\n\n"
                f"Return JSON: {{\"approve\": true/false, "
                f"\"conditions\": [...], \"dissent_reason\": \"...\"}}"
            ),
        )
        response = await self._llm.call(prompt=prompt, role="quality_vote")

        try:
            data = json.loads(response.content)
            return AgentVote(
                agent_name=self.name,
                approve=data.get("approve", False),
                conditions=data.get("conditions", []),
                dissent_reason=data.get("dissent_reason"),
            )
        except json.JSONDecodeError:
            return AgentVote(agent_name=self.name, approve=False,
                             dissent_reason="Could not evaluate completeness")
