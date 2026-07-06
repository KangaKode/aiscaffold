"""
Citation Agent -- deliberation agent that requests evidence level tagging.

Challenges findings that lack proper evidence level tags. Asks "what
evidence level is this? VERIFIED or just INDICATED?" Participates in
Phase 2 (Challenge) to push findings toward clearer evidence grading.

This is a core safety agent. Auto-included unless include_core_agents=False.
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
from ...llm.response_guard import llm_call_failed, parse_agent_json

logger = logging.getLogger(__name__)


class CitationAgent:
    """Requests evidence level tagging on agent findings.

    Phase 1: Lists the evidence levels and what each requires.
    Phase 2: Challenges findings that lack evidence level tags.
    Phase 3: Votes on whether the synthesis properly grades evidence.
    """

    def __init__(self, llm_client=None):
        self._llm = llm_client

    @property
    def name(self) -> str:
        return "citation"

    @property
    def domain(self) -> str:
        return "evidence level tagging and citation review"

    def _system_prompt(self) -> str:
        return (
            "You are a Citation agent. Your job is to ask agents to support "
            "findings with evidence level tags.\n\n"
            "Evidence levels (strongest to weakest):\n"
            "  [VERIFIED: source:reference] -- 'I found this exact data here'\n"
            "  [CORROBORATED: source_1 + source_2] -- 'Multiple sources agree'\n"
            "  [INDICATED: source_name] -- 'One source suggests this, gaps exist'\n"
            "  [POSSIBLE] -- 'Cannot rule out, needs investigation'\n\n"
            "Rules:\n"
            "- Findings should have evidence level tags when possible\n"
            "- VERIFIED requires a specific source:reference (e.g. logs:row_42)\n"
            "- CORROBORATED requires naming 2+ independent sources\n"
            "- Findings without tags should be challenged\n"
            "- Overclaiming (VERIFIED when only INDICATED) is worse than underclaiming\n\n"
            "Always return valid JSON.\n"
        )

    async def analyze(self, task: RoundTableTask) -> AgentAnalysis:
        """Identify what evidence levels should apply to this task."""
        return AgentAnalysis(
            agent_name=self.name,
            domain=self.domain,
            observations=[{
                "finding": "Evidence level review active -- findings should use [VERIFIED/CORROBORATED/INDICATED/POSSIBLE] tags when possible",
                "evidence": "Citation agent monitoring for untagged and overclaimed findings",
                "severity": "info",
                "confidence": 1.0,
            }],
        )

    async def challenge(
        self, task: RoundTableTask, other_analyses: list[AgentAnalysis]
    ) -> AgentChallenge:
        """Challenge findings that lack evidence level tags."""
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
                "For each agent's findings, check:\n"
                "1. Does each finding have an evidence level tag?\n"
                "2. Is the evidence level appropriate (not overclaimed)?\n"
                "3. Do VERIFIED claims cite a specific source:reference?\n"
                "4. Do CORROBORATED claims name 2+ sources?\n\n"
                "Return JSON: {\"challenges\": [{\"target_agent\": ..., "
                "\"finding_challenged\": ..., \"counter_evidence\": ...}], "
                "\"concessions\": [...]}"
            ),
        )
        response = await self._llm.call(prompt=prompt, role="citation_challenge")

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
        """Vote on whether the synthesis properly grades evidence."""
        if not self._llm:
            return AgentVote(agent_name=self.name, approve=False,
                             dissent_reason="Cannot verify citation quality without LLM")

        prompt = CacheablePrompt(
            system=self._system_prompt(),
            user_message=(
                f"Are findings in this synthesis properly tagged with evidence levels?\n\n"
                f"Key findings: {json.dumps(synthesis.key_findings[:5], default=str)}\n\n"
                f"Return JSON: {{\"approve\": true/false, "
                f"\"conditions\": [...], \"dissent_reason\": \"...\"}}"
            ),
        )
        response = await self._llm.call(prompt=prompt, role="citation_vote")

        data = parse_agent_json(response)
        if data is None:
            reason = (
                "Cannot verify citation quality (LLM call failed)"
                if llm_call_failed(response)
                else "Could not evaluate citation quality"
            )
            return AgentVote(agent_name=self.name, approve=False,
                             dissent_reason=reason)
        return AgentVote(
            agent_name=self.name,
            approve=data.get("approve", False),
            conditions=data.get("conditions", []),
            dissent_reason=data.get("dissent_reason"),
        )
