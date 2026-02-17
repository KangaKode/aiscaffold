"""
Evidence Agent -- Claim verification and source validation.

Evaluates whether findings are grounded in evidence, flags speculative
claims presented as facts, and checks citation quality. Distinguishes
fact from inference and grades evidence strength.

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


class EvidenceAgent:
    """Verifies claims are grounded in evidence, not speculation.

    Phase 1 (Analysis): Evaluates what evidence exists in the task.
    Phase 2 (Challenge): Flags unsupported claims from other agents.
    Phase 3 (Voting): Votes on evidence quality in the synthesis.
    """

    def __init__(self, llm_client=None):
        self._llm = llm_client

    @property
    def name(self) -> str:
        return "evidence"

    @property
    def domain(self) -> str:
        return "claim verification and source validation"

    def _system_prompt(self) -> str:
        return (
            "You are an Evidence agent focused on claim verification.\n\n"
            "Your role:\n"
            "- Grade evidence strength: strong (direct data/quotes), moderate "
            "(reasonable inference), weak (speculation/opinion)\n"
            "- Flag claims presented as facts that are actually inferences\n"
            "- Check if cited evidence actually supports the conclusion drawn\n"
            "- Identify circular reasoning (claim supports itself)\n"
            "- Distinguish correlation from causation\n\n"
            "Rules:\n"
            "- Grade evidence, not conclusions -- a wrong conclusion from "
            "strong evidence is different from a right conclusion from no evidence\n"
            "- 'No evidence' is not the same as 'wrong' -- flag it as "
            "unverified, not false\n"
            "- Always return valid JSON\n"
        )

    async def analyze(self, task: RoundTableTask) -> AgentAnalysis:
        """Evaluate what evidence is available in the task."""
        if not self._llm:
            return AgentAnalysis(
                agent_name=self.name,
                domain=self.domain,
                observations=[{
                    "finding": "Evidence not evaluated (no LLM available)",
                    "evidence": "Evidence agent requires LLM to grade claims",
                    "severity": "warning",
                    "confidence": 0.5,
                }],
            )

        prompt = CacheablePrompt(
            system=self._system_prompt(),
            user_message=(
                f"Evaluate the evidence available in this task. Identify:\n"
                f"- What claims are made?\n"
                f"- What evidence supports each claim?\n"
                f"- What claims lack evidence?\n\n"
                f"Task:\n{task.content}\n\n"
                f"Return JSON: {{\"observations\": [{{\"finding\": ..., "
                f"\"evidence\": ..., \"severity\": ..., \"confidence\": ...}}], "
                f"\"recommendations\": [...]}}"
            ),
        )
        response = await self._llm.call(prompt=prompt, role="evidence_analysis")

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
                    "evidence": "raw evidence response",
                    "severity": "info",
                    "confidence": 0.5,
                }],
            )

    async def challenge(
        self, task: RoundTableTask, other_analyses: list[AgentAnalysis]
    ) -> AgentChallenge:
        """Challenge claims that lack adequate evidence."""
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
                "Grade the evidence quality of each agent's findings.\n"
                "Challenge any finding where:\n"
                "- The evidence doesn't actually support the conclusion\n"
                "- The claim is presented as fact but is actually inference\n"
                "- The evidence is circular or self-referencing\n\n"
                "Return JSON: {\"challenges\": [{\"target_agent\": ..., "
                "\"finding_challenged\": ..., \"counter_evidence\": ...}], "
                "\"concessions\": [{\"target_agent\": ..., "
                "\"finding_accepted\": ..., \"reason\": ...}]}"
            ),
        )
        response = await self._llm.call(prompt=prompt, role="evidence_challenge")

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
        """Vote on evidence quality in the synthesis."""
        if not self._llm:
            return AgentVote(agent_name=self.name, approve=False,
                             dissent_reason="Cannot verify evidence without LLM")

        prompt = CacheablePrompt(
            system=self._system_prompt(),
            user_message=(
                f"Grade the evidence quality of this synthesis:\n\n"
                f"Recommendation: {synthesis.recommended_direction}\n"
                f"Key findings: {json.dumps(synthesis.key_findings[:5], default=str)}\n\n"
                f"Vote APPROVE if findings are well-evidenced.\n"
                f"Vote DISSENT if critical claims lack evidence.\n\n"
                f"Return JSON: {{\"approve\": true/false, "
                f"\"conditions\": [...], \"dissent_reason\": \"...\"}}"
            ),
        )
        response = await self._llm.call(prompt=prompt, role="evidence_vote")

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
                             dissent_reason="Could not verify evidence quality")
