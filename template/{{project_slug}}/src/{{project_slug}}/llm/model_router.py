"""
Model router -- heuristic tier selection with cascade and a circuit breaker.

Maps a call role to a cost tier (see model_config.py), picks a model from
that tier, and supports:
  - cascade: on a model failure, retry one tier up (more capable)
  - circuit breaker: if a tier keeps failing, stop routing to it for a
    cooldown window so calls skip straight to a healthier tier
  - budget awareness: when a tenant's remaining budget is low, downgrade one
    tier (cheaper) unless the role was matched by an explicit heuristic

This is a vanilla, dependency-light version: the tier map is configurable
via env (model_config.py), the budget manager is duck-typed and optional,
and there is no hardcoded price table. Wire it into your LLM call path by
asking route() for a model per call; nothing here makes network calls.

Closes the "model diversity" extension point in GOVERNANCE.md: run cheaper
tiers for routine roles and reserve capable tiers (and, if you configure
it, a different provider) for analysis and security screening.
"""

import logging
import os
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .model_config import (
    DEFAULT_TIER,
    SHORT_PROMPT_CHARS,
    TIER_ORDER,
    get_role_tier_map,
    models_in_tier,
)

logger = logging.getLogger(__name__)

BUDGET_DOWNGRADE_PCT = 0.20
CIRCUIT_FAILURE_THRESHOLD = 5
CIRCUIT_COOLDOWN_S = 900


@dataclass
class RoutingDecision:
    """The model chosen for one call, plus how it was chosen."""

    model: str
    tier: str
    method: str  # "override" | "heuristic" | "cascade" | "default"


class ModelRouter:
    """Route call roles to models across cost tiers.

    budget_manager: optional, duck-typed. If it exposes
        remaining_pct(tenant_id) -> float in [0, 1], the router downgrades a
        tier when the tenant is nearly out of budget.
    on_route: optional callback invoked with each RoutingDecision (metrics).
    """

    def __init__(
        self,
        budget_manager: Any | None = None,
        on_route: Callable[[RoutingDecision], None] | None = None,
    ):
        self._budget_manager = budget_manager
        self._on_route = on_route
        self._failures: defaultdict[str, list[float]] = defaultdict(list)
        self._circuit_open_until: dict[str, float] = {}

    def route(
        self,
        role: str,
        prompt: str | None = None,
        model_override: str | None = None,
        tenant_id: str = "default",
    ) -> RoutingDecision:
        """Choose a model for a call role."""
        if model_override:
            return self._emit(RoutingDecision(model_override, self._tier_of(model_override), "override"))

        tier, matched = self._heuristic_tier(role, prompt)
        tier = self._apply_budget(tier, matched, tenant_id)
        tier = self._skip_open_circuits(tier)

        model = self._pick_model(tier)
        if model is None:
            # Walk up to the first tier that has a configured model.
            for candidate in TIER_ORDER[TIER_ORDER.index(tier) + 1:]:
                model = self._pick_model(candidate)
                if model:
                    tier = candidate
                    break
        if model is None:
            model = self._first_available_model()
            tier = self._tier_of(model)
            return self._emit(RoutingDecision(model, tier, "default"))

        return self._emit(RoutingDecision(model, tier, "heuristic" if matched else "default"))

    def cascade(self, role: str, failed_model: str) -> RoutingDecision | None:
        """After a failure, route one tier up. Records the failure for the
        circuit breaker. Returns None when already at the top tier."""
        tier = self._tier_of(failed_model)
        self._record_failure(tier)
        idx = TIER_ORDER.index(tier) if tier in TIER_ORDER else len(TIER_ORDER) - 1
        if idx >= len(TIER_ORDER) - 1:
            return None
        next_tier = TIER_ORDER[idx + 1]
        model = self._pick_model(next_tier)
        if model is None:
            return None
        logger.info("[ModelRouter] Cascade %s: %s -> %s (%s)", role, failed_model, model, next_tier)
        return self._emit(RoutingDecision(model, next_tier, "cascade"))

    def _heuristic_tier(self, role: str, prompt: str | None) -> tuple[str, bool]:
        """Return (tier, matched_by_rule). matched=False means DEFAULT_TIER."""
        role_map = get_role_tier_map()
        if role in role_map:
            return role_map[role], True
        if role.endswith("_vote"):
            return "nano", True
        if role.endswith("_analysis") or role.endswith("_challenge"):
            return "standard", True
        if prompt is not None and len(prompt) < SHORT_PROMPT_CHARS:
            return "nano", True
        return DEFAULT_TIER, False

    def _apply_budget(self, tier: str, matched: bool, tenant_id: str) -> str:
        """Downgrade one tier when the tenant's budget is nearly spent.

        Never downgrades a role that matched an explicit heuristic (those
        tiers were chosen deliberately, e.g. sentinel_analysis -> frontier).
        """
        if matched or self._budget_manager is None:
            return tier
        remaining = self._remaining_pct(tenant_id)
        if remaining is None or remaining >= BUDGET_DOWNGRADE_PCT:
            return tier
        idx = TIER_ORDER.index(tier) if tier in TIER_ORDER else TIER_ORDER.index(DEFAULT_TIER)
        if idx > 0:
            logger.debug("[ModelRouter] Budget downgrade to %s (tenant=%s)", TIER_ORDER[idx - 1], tenant_id)
            return TIER_ORDER[idx - 1]
        return tier

    def _remaining_pct(self, tenant_id: str) -> float | None:
        fn = getattr(self._budget_manager, "remaining_pct", None)
        if fn is None:
            return None
        try:
            return float(fn(tenant_id))
        except Exception:
            return None

    def _skip_open_circuits(self, tier: str) -> str:
        """If the target tier's circuit is open, move up to a healthy tier."""
        idx = TIER_ORDER.index(tier) if tier in TIER_ORDER else TIER_ORDER.index(DEFAULT_TIER)
        for candidate in TIER_ORDER[idx:]:
            if not self._is_circuit_open(candidate):
                return candidate
        return tier

    def _pick_model(self, tier: str) -> str | None:
        models = models_in_tier(tier)
        return models[0] if models else None

    def _first_available_model(self) -> str:
        for tier in TIER_ORDER:
            model = self._pick_model(tier)
            if model:
                return model
        return "unknown"

    def _tier_of(self, model: str) -> str:
        for tier in TIER_ORDER:
            if model in models_in_tier(tier):
                return tier
        return DEFAULT_TIER

    def _record_failure(self, tier: str) -> None:
        now = time.monotonic()
        window = [t for t in self._failures[tier] if t > now - CIRCUIT_COOLDOWN_S]
        window.append(now)
        self._failures[tier] = window
        if len(window) >= CIRCUIT_FAILURE_THRESHOLD:
            self._circuit_open_until[tier] = now + CIRCUIT_COOLDOWN_S
            logger.info("[ModelRouter] Circuit breaker opened for tier=%s", tier)

    def _is_circuit_open(self, tier: str) -> bool:
        return time.monotonic() < self._circuit_open_until.get(tier, 0.0)

    def _emit(self, decision: RoutingDecision) -> RoutingDecision:
        if self._on_route:
            try:
                self._on_route(decision)
            except Exception:
                pass
        return decision


def create_model_router(budget_manager: Any | None = None) -> ModelRouter | None:
    """Build a router when MODEL_ROUTING_ENABLED is truthy, else None.

    Returning None lets callers keep the zero-config single-model path
    unchanged unless routing is explicitly turned on.
    """
    if os.environ.get("MODEL_ROUTING_ENABLED", "").strip().lower() not in ("true", "1", "yes"):
        return None
    return ModelRouter(budget_manager=budget_manager)


# =============================================================================
# LLM CLIENT GLUE (kept here so client.py stays small)
# =============================================================================


def route_for_call(router: Any, role: str, prompt_text: str, default_model: str) -> str:
    """Pick the model for one LLM call via the router (client glue).

    Tenant identity reuses the same contextvar the client's budget
    enforcement reads (set at orchestration entry points), so
    budget-aware downgrades apply to the caller's tenant. Never raises:
    any routing error falls back to default_model -- routing selects
    models, it must never break a call.
    """
    if router is None:
        return default_model
    try:
        from .budget_manager import get_tenant_context

        return router.route(
            role=role, prompt=prompt_text, tenant_id=get_tenant_context()
        ).model
    except Exception as exc:
        logger.warning(
            "[ModelRouter] route() failed (%s); using default model",
            type(exc).__name__,
        )
        return default_model


def cascade_for_call(router: Any, role: str, failed_model: str) -> str | None:
    """One cascade step after a final call failure (client glue).

    Returns the next (more capable) model, or None when there is no
    router, no higher tier, or the cascade itself errors. Never raises.
    """
    if router is None:
        return None
    try:
        decision = router.cascade(role, failed_model)
        return decision.model if decision is not None else None
    except Exception as exc:
        logger.warning(
            "[ModelRouter] cascade() failed (%s); no fallback attempted",
            type(exc).__name__,
        )
        return None
