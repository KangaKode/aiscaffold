"""
FactChecker Agent -- deliberation agent that challenges speculation in round table.

Unlike the enforcement pipeline validators, this agent explains why
speculation language is problematic and suggests evidence-based rewrites.
Participates in Phase 2 (Challenge) to educate other agents.

This is a core safety agent. Auto-included unless include_core_agents=False.
"""

import json
import logging

from ...enforcement.fact_checker import BANNED_PATTERNS
from ...llm import CacheablePrompt
from ...orchestration.round_table import (
    AgentAnalysis,
    AgentChallenge,
    AgentVote,
    RoundTableTask,
    SynthesisResult,
)
from ...llm.response_guard import llm_call_failed, parse_agent_json

logger = logging.getLogger(__name__)


class FactCheckerAgent:
    """Challenges speculation and opinion in other agents' responses.

    Phase 1: Scans task for claims that will need evidence.
    Phase 2: Challenges agents who used banned speculation patterns.
    Phase 3: Votes on whether the synthesis avoids speculation.
    """

    def __init__(self, llm_client=None):
        self._llm = llm_client

    @property
    def name(self) -> str:
        return "fact_checker"

    @property
    def domain(self) -> str:
        return "speculation detection and evidence review"

    def _system_prompt(self) -> str:
        banned_examples = []
        for category, patterns in BANNED_PATTERNS.items():
            for p in patterns[:2]:
                banned_examples.append(f"  - {p['message']}")

        return (
            "You are a FactChecker agent. Your job is to ensure all findings "
            "are evidence-based, not speculative.\n\n"
            "BANNED language (challenge any agent using these):\n"
            + "\n".join(banned_examples) + "\n\n"
            "PREFERRED: Findings should use evidence level tags when possible:\n"
            "  [VERIFIED: source:reference] -- direct proof\n"
            "  [CORROBORATED: source_1 + source_2] -- multiple sources agree\n"
            "  [INDICATED: source_name] -- single source, gaps acknowledged\n"
            "  [POSSIBLE] -- cannot confirm, explains what would verify\n\n"
            "Always return valid JSON.\n"
        )

    async def analyze(self, task: RoundTableTask) -> AgentAnalysis:
        """Identify what evidence will be needed for this task."""
        return AgentAnalysis(
            agent_name=self.name,
            domain=self.domain,
            observations=[{
                "finding": "Evidence review active -- findings should use evidence level tags when possible",
                "evidence": "FactChecker monitoring for speculation, opinions, and hedging",
                "severity": "info",
                "confidence": 1.0,
            }],
        )

    async def challenge(
        self, task: RoundTableTask, other_analyses: list[AgentAnalysis]
    ) -> AgentChallenge:
        """Challenge agents who used banned speculation patterns."""
        if not self._llm or not other_analyses:
            return AgentChallenge(agent_name=self.name)

        analyses_text = json.dumps(
            [{"agent": a.agent_name, "findings": a.observations[:5]}
             for a in other_analyses if a.agent_name != self.name],
            indent=2, default=str,
        )

        prompt = CacheablePrompt(
            system=self._system_prompt(),
            context=f"Other agents' analyses:\n{analyses_text}",
            user_message=(
                "Check each agent's findings for:\n"
                "1. Speculation language (probably, likely, suggests, appears)\n"
                "2. Opinion statements (I think, I believe)\n"
                "3. Missing evidence level tags\n"
                "4. Claims without source citations\n\n"
                "For each violation, explain WHY it's problematic and suggest "
                "a specific rewrite using evidence level tags.\n\n"
                "Return JSON: {\"challenges\": [{\"target_agent\": ..., "
                "\"finding_challenged\": ..., \"counter_evidence\": ...}], "
                "\"concessions\": [...]}"
            ),
        )
        response = await self._llm.call(prompt=prompt, role="fact_checker_challenge")

        data = parse_agent_json(response)
        if data is None:
            return AgentChallenge(agent_name=self.name)
        return AgentChallenge(
            agent_name=self.name,
            challenges=data.get("challenges", []),
            concessions=data.get("concessions", []),
        )

    async def vote(
        self, task: RoundTableTask, synthesis: SynthesisResult
    ) -> AgentVote:
        """Vote on whether the synthesis avoids speculation."""
        if not self._llm:
            return AgentVote(agent_name=self.name, approve=False,
                             dissent_reason="Cannot verify evidence quality without LLM")

        prompt = CacheablePrompt(
            system=self._system_prompt(),
            user_message=(
                f"Does this synthesis avoid speculation and use evidence levels?\n\n"
                f"Recommendation: {synthesis.recommended_direction}\n"
                f"Key findings: {json.dumps(synthesis.key_findings[:5], default=str)}\n\n"
                f"Return JSON: {{\"approve\": true/false, "
                f"\"conditions\": [...], \"dissent_reason\": \"...\"}}"
            ),
        )
        response = await self._llm.call(prompt=prompt, role="fact_checker_vote")

        data = parse_agent_json(response)
        if data is None:
            reason = (
                "Cannot verify evidence quality (LLM call failed)"
                if llm_call_failed(response)
                else "Could not evaluate evidence quality"
            )
            return AgentVote(agent_name=self.name, approve=False,
                             dissent_reason=reason)
        return AgentVote(
            agent_name=self.name,
            approve=data.get("approve", False),
            conditions=data.get("conditions", []),
            dissent_reason=data.get("dissent_reason"),
        )
