"""
ChatOrchestrator -- Lightweight multi-agent chat with hallucination resistance.

Implements the Orchestrator-Worker pattern (2026 best practices Section 4.1):
a lead agent drives the conversation, selects 1-3 specialists who provide
evidence-backed responses (same evidence requirement as the round table),
cross-checks them for agreement, surfaces disagreements with evidence, and
suggests escalation to the round table for complex queries.

Token efficiency: CacheablePrompt caches the system prompt across messages;
only relevant specialists are consulted; single synthesis pass.
Security: specialist responses sanitized before synthesis; input validated
and size-limited; same injection defense as the round table; safety agents
join every consultation; synthesis FactChecked before returning to the user.

Keep this file under 500 lines (helpers live in chat_helpers.py).
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..llm import CacheablePrompt, LLMClient
from ..llm.budget_manager import set_tenant_context
from ..security.prompt_guard import sanitize_for_prompt, wrap_user_content
from .agent_router import AgentRouter, RoutingDecision
from .autonomy import resolve_policy

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
    enforcement_result: str = ""
    enforcement_violations: list[str] = field(default_factory=list)


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
    include_safety_agents: bool = True


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
        baseline_tracker: Any = None,
    ):
        self._llm = llm
        self._registry = registry
        self._router = router or AgentRouter(registry=registry)
        self._config = config or ChatConfig()
        # Opt-in per-dispatch baseline stats via the env-gated factory
        # learning.activity.create_baseline_tracker (None = off, unchanged).
        self._baseline_tracker = baseline_tracker
        self._conversation_history: list[dict] = []

    def _system_prompt(self, tenant_id: str = "default") -> str:
        """Stable orchestrator system prompt (cached for token savings)."""
        agent_info = ""
        if self._registry and self._registry.count > 0:
            agents = self._registry.list_for_tenant(tenant_id)
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
        tenant_id: str = "default",
        autonomy_level: int | None = None,
    ) -> ChatResponse:
        """
        Process a chat message with selective specialist consultation.

        Args:
            message: The user's message.
            trust_scores: Optional agent trust scores from the learning system.
            context: Optional additional context (e.g., user preferences).
            tenant_id: Tenant scope -- only tenant-visible agents are consulted.
            autonomy_level: Optional autonomy level (1-6). The resolved
                policy caps specialist count and may force escalation on
                specialist conflict.

        Returns:
            ChatResponse with content, consultations, and cross-check results.
        """
        start = datetime.now()
        set_tenant_context(tenant_id)

        policy = resolve_policy(autonomy_level) if autonomy_level is not None else None
        effective_max_agents = self._config.max_agents
        if policy is not None:
            effective_max_agents = min(effective_max_agents, policy.max_specialists)

        routing = self._router.route(
            message, trust_scores=trust_scores, tenant_id=tenant_id
        )

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

        consultation_agents = list(routing.selected_agents)
        consultation_agents = consultation_agents[:effective_max_agents]
        for sa in self._get_safety_agents():
            if sa.name not in [a.name for a in consultation_agents]:
                consultation_agents.append(sa)

        consultations = []
        if consultation_agents:
            consultations = await self._consult_specialists(
                message, consultation_agents, tenant_id=tenant_id
            )

        cross_check = None
        if self._config.enable_cross_check and len(consultations) > 1:
            cross_check = await self._cross_check(consultations)

        response_content = await self._synthesize(
            message, consultations, cross_check, context, tenant_id
        )

        enforcement_result = "accepted"
        enforcement_violations: list[str] = []
        if consultations:
            (
                response_content,
                enforcement_result,
                enforcement_violations,
            ) = await self._enforce_synthesis(
                response_content, message, consultations,
                cross_check, context, tenant_id,
            )

        escalation_suggested = False
        escalation_reason = ""

        if enforcement_result == "rejected":
            escalation_suggested = True
            escalation_reason = (
                "Enforcement rejected synthesis; "
                "consider the round table for deeper analysis"
            )

        if cross_check and cross_check.should_escalate:
            escalation_suggested = True
            escalation_reason = escalation_reason or cross_check.escalation_reason

        if routing.should_escalate:
            escalation_suggested = True
            escalation_reason = escalation_reason or routing.escalation_reason

        conflicts_found = cross_check is not None and bool(cross_check.conflicts)
        if policy is not None and policy.auto_escalate_on_conflict and conflicts_found:
            escalation_suggested = True
            escalation_reason = escalation_reason or (
                f"Autonomy level {policy.level} escalates on specialist "
                f"conflict ({len(cross_check.conflicts)} conflict(s) found)"
            )

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
            enforcement_result=enforcement_result,
            enforcement_violations=enforcement_violations,
        )

    def _get_safety_agents(self) -> list:
        """Return evidence + skeptic + sentinel agents for mandatory chat oversight."""
        if not self._config.include_safety_agents:
            return []
        try:
            from ..agents.core import get_core_agents

            core = get_core_agents(llm_client=self._llm)
            return [
                a for a in core
                if a.name in ("evidence", "skeptic", "sentinel")
            ]
        except Exception:
            logger.warning("[ChatOrchestrator] Could not load safety agents")
            return []

    async def _consult_specialists(
        self,
        message: str,
        agents: list,
        tenant_id: str = "default",
    ) -> list[ConsultationResult]:
        """Consult selected specialists in parallel.

        Each specialist passes identity, rate-limit, and scope gates before
        the consultation (gated specialists just aren't consulted). A wired
        baseline tracker records per-dispatch stats in the caller's tenant
        (detect-only, fire-and-forget).
        """
        from ..orchestration.round_table import RoundTableTask
        from .dispatch_helpers import dispatch_with_gates

        task = RoundTableTask(
            id=f"chat_{datetime.now().strftime('%H%M%S')}",
            content=message,
            tenant_id=tenant_id,
        )

        rate_limiter = getattr(self._registry, "rate_limiter", None)
        analyses, _skipped, _failed = await dispatch_with_gates(
            agents, task, self._registry, rate_limiter, "ChatOrchestrator",
            baseline_tracker=self._baseline_tracker,
        )

        consultations = []
        for result in analyses:
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
            user_message=wrap_user_content(
                consultation_summary, label="SPECIALIST_RESPONSES"
            ),
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

    async def _enforce_synthesis(
        self,
        content: str,
        message: str,
        consultations: list[ConsultationResult],
        cross_check: CrossCheckResult | None,
        context: str,
        tenant_id: str,
    ) -> tuple[str, str, list[str]]:
        """Run FactChecker on synthesis. Delegates to chat_helpers."""
        from .chat_helpers import enforce_chat_synthesis

        async def _re_synthesize(correction: str = "") -> str:
            return await self._synthesize(
                message, consultations, cross_check,
                context, tenant_id, correction=correction,
            )

        return await enforce_chat_synthesis(content, _re_synthesize)

    async def _synthesize(
        self,
        message: str,
        consultations: list[ConsultationResult],
        cross_check: CrossCheckResult | None,
        context: str,
        tenant_id: str = "default",
        correction: str = "",
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

        correction_note = f"\nCORRECTION: {correction}\n" if correction else ""

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
            system=self._system_prompt(tenant_id=tenant_id),
            context=(
                f"{f'User context: {context}' if context else ''}\n\n"
                f"{f'Conversation history:{chr(10)}{history_text}' if history_text else ''}\n\n"
                f"{f'Specialist consultations:{chr(10)}{consultation_text}' if consultation_text else ''}"
                f"{conflict_note}"
                f"{correction_note}"
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

    @property
    def conversation_history(self) -> list[dict]:
        """Read-only copy of conversation history."""
        return list(self._conversation_history)
