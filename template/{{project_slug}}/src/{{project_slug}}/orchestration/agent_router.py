"""
AgentRouter -- Selects which specialists to consult for a given query.

Used by the ChatOrchestrator to pick 1-3 relevant agents instead of
involving all agents (which is the RoundTable's job).

Routing strategies:
  1. Domain matching: match query keywords against agent domains
  2. Trust-weighted: prefer agents with higher trust scores (when learning system is active)
  3. Capability matching: match against agent capability tags

The router is intentionally simple -- a lead agent (LLM) makes the final
decision. This module provides candidate selection; the orchestrator decides.

Keep this file under 200 lines.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGENTS = 3
DEFAULT_MIN_AGENTS = 1


@dataclass
class RoutingDecision:
    """Result of agent routing -- which agents to consult and why."""

    selected_agents: list[Any] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    should_escalate: bool = False
    escalation_reason: str = ""


class AgentRouter:
    """
    Picks which specialists to consult for a chat message.

    Usage:
        router = AgentRouter(registry=registry)
        decision = router.route("How do I optimize database queries?")
        # decision.selected_agents -> [sql_agent, perf_agent]
    """

    def __init__(
        self,
        registry: Any = None,
        max_agents: int = DEFAULT_MAX_AGENTS,
        min_agents: int = DEFAULT_MIN_AGENTS,
    ):
        self._registry = registry
        self._max_agents = max_agents
        self._min_agents = min_agents

    def route(
        self,
        query: str,
        trust_scores: dict[str, float] | None = None,
        tenant_id: str = "default",
    ) -> RoutingDecision:
        """
        Select agents for a query based on domain matching and trust.

        Args:
            query: The user's message.
            trust_scores: Optional {agent_name: score} from learning system.
            tenant_id: Tenant scope -- only tenant-visible agents are candidates.

        Returns:
            RoutingDecision with selected agents and reasoning.
        """
        if self._registry is None or self._registry.count == 0:
            return RoutingDecision(
                should_escalate=True,
                escalation_reason="No agents registered",
            )

        all_entries = self._registry.list_for_tenant(tenant_id)
        scored: list[tuple[float, Any, str]] = []

        query_lower = query.lower()

        for entry in all_entries:
            if not entry.healthy:
                continue

            score = 0.0
            reason_parts = []

            domain_words = entry.agent.domain.lower().split()
            domain_matches = sum(1 for w in domain_words if w in query_lower)
            if domain_matches > 0:
                score += domain_matches * 0.3
                reason_parts.append(f"domain match ({domain_matches} words)")

            for cap in entry.capabilities:
                if cap.lower() in query_lower:
                    score += 0.2
                    reason_parts.append(f"capability: {cap}")

            if trust_scores and entry.agent.name in trust_scores:
                trust = trust_scores[entry.agent.name]
                score += trust * 0.2
                reason_parts.append(f"trust: {trust:.2f}")

            score += 0.1

            reason = ", ".join(reason_parts) if reason_parts else "baseline"
            scored.append((score, entry.agent, reason))

        scored.sort(key=lambda x: x[0], reverse=True)

        selected = scored[: self._max_agents]

        if len(selected) < self._min_agents:
            return RoutingDecision(
                selected_agents=[s[1] for s in selected],
                reasons={s[1].name: s[2] for s in selected},
                confidence=0.2,
                should_escalate=True,
                escalation_reason="Too few healthy agents available",
            )

        avg_score = sum(s[0] for s in selected) / len(selected) if selected else 0
        confidence = min(avg_score / 1.0, 1.0)

        decision = RoutingDecision(
            selected_agents=[s[1] for s in selected],
            reasons={s[1].name: s[2] for s in selected},
            confidence=confidence,
        )

        if confidence < 0.3 and len(scored) > self._max_agents:
            decision.should_escalate = True
            decision.escalation_reason = (
                "Low routing confidence -- consider full round table"
            )

        logger.debug(
            f"[AgentRouter] Selected {len(decision.selected_agents)} agents "
            f"(confidence={confidence:.2f}): "
            f"{[a.name for a in decision.selected_agents]}"
        )
        return decision

    def route_with_llm_hint(
        self,
        query: str,
        llm_suggested_agents: list[str],
        trust_scores: dict[str, float] | None = None,
        tenant_id: str = "default",
    ) -> RoutingDecision:
        """
        Route using LLM-suggested agent names, validated against registry.

        The ChatOrchestrator's lead agent can suggest which specialists to
        consult. This method validates those suggestions against the registry
        and falls back to domain matching for any that don't exist.
        """
        if self._registry is None:
            return self.route(query, trust_scores, tenant_id=tenant_id)

        validated = []
        reasons = {}

        # Validate suggestions against the tenant-visible set only, so an
        # LLM suggestion can never pull in another tenant's private agents.
        visible = {
            entry.agent.name: entry.agent
            for entry in self._registry.list_for_tenant(tenant_id)
        }
        for name in llm_suggested_agents[: self._max_agents]:
            agent = visible.get(name)
            if agent is not None:
                validated.append(agent)
                reasons[name] = "LLM-selected"

        if len(validated) < self._min_agents:
            fallback = self.route(query, trust_scores, tenant_id=tenant_id)
            for agent in fallback.selected_agents:
                if agent.name not in reasons:
                    validated.append(agent)
                    reasons[agent.name] = fallback.reasons.get(
                        agent.name, "fallback"
                    )
                if len(validated) >= self._max_agents:
                    break

        confidence = 0.8 if len(validated) >= self._min_agents else 0.3

        return RoutingDecision(
            selected_agents=validated[: self._max_agents],
            reasons=reasons,
            confidence=confidence,
        )
