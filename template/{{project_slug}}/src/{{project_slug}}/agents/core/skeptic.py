"""
Skeptic Agent -- Devil's advocate for every round table.

Challenges assumptions, demands evidence, flags logical fallacies,
and resists consensus pressure. Promotes critical thinking over
blind acceptance of AI-generated analysis.

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


class SkepticAgent:
    """Devil's advocate that challenges assumptions and demands evidence.

    Phase 1 (Analysis): Identifies assumptions, logical gaps, cognitive biases.
    Phase 2 (Challenge): Pushes back on weak reasoning across all agents.
    Phase 3 (Voting): Votes based on reasoning soundness, not consensus.
    """

    def __init__(self, llm_client=None):
        self._llm = llm_client

    @property
    def name(self) -> str:
        return "skeptic"

    @property
    def domain(self) -> str:
        return "critical thinking and assumption validation"

    def _system_prompt(self) -> str:
        return (
            "You are a Skeptic -- a devil's advocate whose job is to keep "
            "other agents honest.\n\n"
            "Your role:\n"
            "- Challenge assumptions that lack supporting evidence\n"
            "- Identify logical fallacies (confirmation bias, appeal to authority, "
            "false dichotomy, hasty generalization)\n"
            "- Flag claims presented with high confidence but weak evidence\n"
            "- Ask 'what could go wrong?' and 'what are we missing?'\n"
            "- Resist consensus pressure -- dissent is your primary value\n\n"
            "Rules:\n"
            "- Every challenge MUST include counter-evidence or a specific "
            "logical flaw, not just disagreement\n"
            "- Grade reasoning quality, not domain correctness\n"
            "- If an agent's reasoning IS sound, acknowledge it\n"
            "- Always return valid JSON\n"
        )

    async def analyze(self, task: RoundTableTask) -> AgentAnalysis:
        """Identify assumptions and potential blind spots in the task itself."""
        if not self._llm:
            return AgentAnalysis(
                agent_name=self.name,
                domain=self.domain,
                observations=[{
                    "finding": "Task accepted without critical review (no LLM available)",
                    "evidence": "Skeptic agent requires LLM to evaluate assumptions",
                    "severity": "warning",
                    "confidence": 0.5,
                }],
            )

        prompt = CacheablePrompt(
            system=self._system_prompt(),
            user_message=(
                f"Critically evaluate this task for hidden assumptions, "
                f"ambiguities, and potential blind spots:\n\n{task.content}\n\n"
                f"Return JSON: {{\"observations\": [{{\"finding\": ..., "
                f"\"evidence\": ..., \"severity\": ..., \"confidence\": ...}}], "
                f"\"recommendations\": [...]}}"
            ),
        )
        response = await self._llm.call(prompt=prompt, role="skeptic_analysis")

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
                    "evidence": "raw skeptic response",
                    "severity": "info",
                    "confidence": 0.5,
                }],
            )

    async def challenge(
        self, task: RoundTableTask, other_analyses: list[AgentAnalysis]
    ) -> AgentChallenge:
        """Challenge every agent's findings -- this is the skeptic's core job."""
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
                "Challenge these analyses. For each challenge:\n"
                "- Identify the specific finding being challenged\n"
                "- Explain the logical flaw or missing evidence\n"
                "- Suggest what evidence would be needed to support the claim\n\n"
                "Return JSON: {\"challenges\": [{\"target_agent\": ..., "
                "\"finding_challenged\": ..., \"counter_evidence\": ...}], "
                "\"concessions\": [{\"target_agent\": ..., "
                "\"finding_accepted\": ..., \"reason\": ...}]}"
            ),
        )
        response = await self._llm.call(prompt=prompt, role="skeptic_challenge")

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
        """Vote based on reasoning soundness, not consensus pressure."""
        if not self._llm:
            return AgentVote(agent_name=self.name, approve=False,
                             dissent_reason="Cannot evaluate without LLM")

        prompt = CacheablePrompt(
            system=self._system_prompt(),
            user_message=(
                f"Evaluate this synthesis for reasoning quality:\n\n"
                f"Recommendation: {synthesis.recommended_direction}\n"
                f"Key findings: {json.dumps(synthesis.key_findings[:5], default=str)}\n\n"
                f"Vote APPROVE only if the reasoning is sound and evidence-based.\n"
                f"Vote DISSENT if there are logical gaps or unsupported claims.\n\n"
                f"Return JSON: {{\"approve\": true/false, "
                f"\"conditions\": [...], \"dissent_reason\": \"...\"}}"
            ),
        )
        response = await self._llm.call(prompt=prompt, role="skeptic_vote")

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
                             dissent_reason="Could not evaluate synthesis")
