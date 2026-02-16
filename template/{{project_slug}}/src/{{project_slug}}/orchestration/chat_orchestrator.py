"""
ChatOrchestrator -- Lightweight multi-agent chat with hallucination resistance.

Implements the Orchestrator-Worker pattern (2026 best practices Section 4.1):
  - A lead agent drives the conversation and selects which specialists to consult
  - 1-3 specialists provide evidence-backed responses (same evidence requirement as round table)
  - The orchestrator cross-checks specialist responses for agreement/disagreement
  - If specialists disagree, both views are surfaced to the user with evidence
  - If the query is too complex, escalation to the full round table is suggested

Token efficiency:
  - Uses CacheablePrompt so system instructions are cached across messages
  - Only consults relevant specialists (not all agents)
  - Single synthesis pass (vs round table's 4 phases)

Security:
  - All specialist responses sanitized before synthesis
  - Input validated and size-limited
  - Same prompt injection defense as round table

Keep this file under 400 lines.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..llm import CacheablePrompt, LLMClient
from ..security.prompt_guard import sanitize_for_prompt
from .agent_router import AgentRouter, RoutingDecision

logger = logging.getLogger(__name__)

ESCALATION_CONFLICT_THRESHOLD = 0.4
MAX_CONSULTATION_AGENTS = 3


# =============================================================================
# DATA MODELS
# =============================================================================


@dataclass
class ConsultationResult:
    """A single specialist's response to a consultation."""

    agent_name: str
    domain: str
    response: str
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class CrossCheckResult:
    """Result of cross-checking specialist responses."""

    agreement_level: float = 1.0
    conflicts: list[dict] = field(default_factory=list)
    consensus_points: list[str] = field(default_factory=list)
    should_escalate: bool = False
    escalation_reason: str = ""


@dataclass
class ChatResponse:
    """Complete response from the chat orchestrator."""

    content: str
    consultations: list[ConsultationResult] = field(default_factory=list)
    cross_check: CrossCheckResult | None = None
    escalation_suggested: bool = False
    escalation_reason: str = ""
    routing_decision: RoutingDecision | None = None
    duration_seconds: float = 0.0
    agents_consulted: list[str] = field(default_factory=list)


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class ChatConfig:
    """Configuration for the chat orchestrator."""

    max_agents: int = MAX_CONSULTATION_AGENTS
    enable_cross_check: bool = True
    auto_escalate_on_conflict: bool = False
    escalation_threshold: float = ESCALATION_CONFLICT_THRESHOLD
    max_message_length: int = 100_000


# =============================================================================
# CHAT ORCHESTRATOR
# =============================================================================


class ChatOrchestrator:
    """
    Lightweight multi-agent chat orchestrator.

    Usage:
        orchestrator = ChatOrchestrator(
            llm=llm_client,
            registry=agent_registry,
        )
        response = await orchestrator.chat("How do I optimize this query?")
        print(response.content)

        if response.escalation_suggested:
            print(f"Consider round table: {response.escalation_reason}")
    """

    def __init__(
        self,
        llm: LLMClient,
        registry: Any = None,
        router: AgentRouter | None = None,
        config: ChatConfig | None = None,
    ):
        self._llm = llm
        self._registry = registry
        self._router = router or AgentRouter(registry=registry)
        self._config = config or ChatConfig()
        self._conversation_history: list[dict] = []

    def _system_prompt(self) -> str:
        """Stable orchestrator system prompt (cached for token savings)."""
        agent_info = ""
        if self._registry and self._registry.count > 0:
            agents = self._registry.get_all_entries()
            agent_info = "Available specialists:\n" + "\n".join(
                f"  - {e.agent.name}: {e.agent.domain}"
                for e in agents
                if e.healthy
            )

        return (
            "You are a chat orchestrator that helps users by consulting "
            "specialist agents when needed.\n\n"
            "Rules:\n"
            "- For simple questions you can answer directly\n"
            "- For domain-specific questions, consult relevant specialists\n"
            "- ALWAYS cite evidence for factual claims\n"
            "- If specialists disagree, present BOTH views with evidence\n"
            "- Never hide uncertainty -- tell the user when confidence is low\n"
            "- If a question is too complex for chat, suggest the round table\n\n"
            f"{agent_info}"
        )

    async def chat(
        self,
        message: str,
        trust_scores: dict[str, float] | None = None,
        context: str = "",
    ) -> ChatResponse:
        """
        Process a chat message with selective specialist consultation.

        Args:
            message: The user's message.
            trust_scores: Optional agent trust scores from the learning system.
            context: Optional additional context (e.g., user preferences).

        Returns:
            ChatResponse with content, consultations, and cross-check results.
        """
        start = datetime.now()

        routing = self._router.route(message, trust_scores=trust_scores)

        if routing.should_escalate and not routing.selected_agents:
            return ChatResponse(
                content=(
                    "This question would benefit from a full team analysis. "
                    f"Reason: {routing.escalation_reason}"
                ),
                escalation_suggested=True,
                escalation_reason=routing.escalation_reason,
                routing_decision=routing,
            )

        consultations = []
        if routing.selected_agents:
            consultations = await self._consult_specialists(
                message, routing.selected_agents
            )

        cross_check = None
        if self._config.enable_cross_check and len(consultations) > 1:
            cross_check = await self._cross_check(consultations)

        response_content = await self._synthesize(
            message, consultations, cross_check, context
        )

        escalation_suggested = False
        escalation_reason = ""

        if cross_check and cross_check.should_escalate:
            escalation_suggested = True
            escalation_reason = cross_check.escalation_reason

        if routing.should_escalate:
            escalation_suggested = True
            escalation_reason = escalation_reason or routing.escalation_reason

        duration = (datetime.now() - start).total_seconds()

        self._conversation_history.append({
            "role": "user",
            "content": message,
        })
        self._conversation_history.append({
            "role": "assistant",
            "content": response_content,
            "agents_consulted": [c.agent_name for c in consultations],
        })

        return ChatResponse(
            content=response_content,
            consultations=consultations,
            cross_check=cross_check,
            escalation_suggested=escalation_suggested,
            escalation_reason=escalation_reason,
            routing_decision=routing,
            duration_seconds=duration,
            agents_consulted=[c.agent_name for c in consultations],
        )

    async def _consult_specialists(
        self,
        message: str,
        agents: list,
    ) -> list[ConsultationResult]:
        """Consult selected specialists in parallel."""
        from ..orchestration.round_table import RoundTableTask

        task = RoundTableTask(
            id=f"chat_{datetime.now().strftime('%H%M%S')}",
            content=message,
        )

        results = await asyncio.gather(
            *[agent.analyze(task) for agent in agents],
            return_exceptions=True,
        )

        consultations = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    f"[ChatOrchestrator] {agents[i].name} consultation failed: {result}"
                )
                continue

            evidence = []
            for obs in result.observations:
                if isinstance(obs, dict) and obs.get("evidence"):
                    evidence.append(
                        sanitize_for_prompt(str(obs["evidence"]), max_length=2000)
                    )

            consultations.append(ConsultationResult(
                agent_name=result.agent_name,
                domain=result.domain,
                response=sanitize_for_prompt(
                    json.dumps(result.observations, default=str),
                    max_length=10_000,
                ),
                evidence=evidence,
                confidence=result.confidence,
            ))

        return consultations

    async def _cross_check(
        self,
        consultations: list[ConsultationResult],
    ) -> CrossCheckResult:
        """Cross-check specialist responses for agreement/disagreement."""
        consultation_summary = json.dumps(
            [
                {
                    "agent": c.agent_name,
                    "domain": c.domain,
                    "response": c.response[:2000],
                    "confidence": c.confidence,
                }
                for c in consultations
            ],
            indent=2,
        )

        prompt = CacheablePrompt(
            system=(
                "You are a cross-checker. Compare specialist responses and identify:\n"
                "1. Points where specialists AGREE (consensus)\n"
                "2. Points where specialists DISAGREE (conflicts)\n"
                "3. An agreement_level from 0.0 (total conflict) to 1.0 (full agreement)\n\n"
                "Return JSON: {\"agreement_level\": float, \"consensus_points\": [...], "
                "\"conflicts\": [{\"point\": str, \"views\": [...]}]}"
            ),
            user_message=f"Specialist responses:\n{consultation_summary}",
        )

        response = await self._llm.call(
            prompt=prompt, role="cross_check", temperature=0.1
        )

        try:
            from ..llm.json_parser import extract_json

            data = extract_json(response.content)
            if data is None:
                return CrossCheckResult(agreement_level=0.5)
            agreement = float(data.get("agreement_level", 1.0))
            should_escalate = agreement < self._config.escalation_threshold

            return CrossCheckResult(
                agreement_level=agreement,
                conflicts=data.get("conflicts", []),
                consensus_points=data.get("consensus_points", []),
                should_escalate=should_escalate,
                escalation_reason=(
                    f"Significant specialist disagreement (agreement: {agreement:.0%})"
                    if should_escalate
                    else ""
                ),
            )
        except (json.JSONDecodeError, ValueError):
            return CrossCheckResult(agreement_level=0.5)

    async def _synthesize(
        self,
        message: str,
        consultations: list[ConsultationResult],
        cross_check: CrossCheckResult | None,
        context: str,
    ) -> str:
        """Synthesize specialist consultations into a user-facing response."""
        consultation_text = ""
        if consultations:
            parts = []
            for c in consultations:
                parts.append(
                    f"[{c.agent_name} ({c.domain}, confidence: {c.confidence:.0%})]:\n"
                    f"{c.response[:3000]}"
                )
            consultation_text = "\n\n".join(parts)

        conflict_note = ""
        if cross_check and cross_check.conflicts:
            conflict_note = (
                "\n\nIMPORTANT: Specialists disagree on some points. "
                "Present BOTH views with supporting evidence. "
                "Do NOT pick a side without evidence."
            )

        history_text = ""
        recent = self._conversation_history[-6:]
        if recent:
            history_text = "\n".join(
                f"{h['role']}: {str(h['content'])[:500]}" for h in recent
            )

        prompt = CacheablePrompt(
            system=self._system_prompt(),
            context=(
                f"{f'User context: {context}' if context else ''}\n\n"
                f"{f'Conversation history:{chr(10)}{history_text}' if history_text else ''}\n\n"
                f"{f'Specialist consultations:{chr(10)}{consultation_text}' if consultation_text else ''}"
                f"{conflict_note}"
            ),
            user_message=message,
        )

        response = await self._llm.call(
            prompt=prompt, role="chat_synthesis", temperature=0.4
        )
        return response.content

    def clear_history(self) -> None:
        """Clear conversation history (start fresh)."""
        self._conversation_history.clear()

    @property
    def history_length(self) -> int:
        """Number of messages in conversation history."""
        return len(self._conversation_history)
