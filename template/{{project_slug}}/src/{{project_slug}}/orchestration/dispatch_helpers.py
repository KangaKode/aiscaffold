"""Shared dispatch helpers for agent sandboxing.

Standalone functions used by orchestrators around each agent call:
identity verification, capability resolution, rate limiting, scope
filtering, and call duration logging.
Registry and rate limiter are passed as arguments (no globals).
"""

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any

from ..agents.capability import AgentCapability
from ..agents.rate_limiter import DEFAULT_MAX_CALLS_PER_HOUR, AgentRateLimiter
from .identity_check import verify_agent_identity
from .scope_filter import ScopeFilter

logger = logging.getLogger(__name__)


def get_capability(registry: Any, agent: Any) -> AgentCapability | None:
    """Resolve agent capability -- prefer registry entry over agent instance."""
    capability = None
    if registry:
        entry = registry.get_entry(agent.name)
        if entry:
            capability = getattr(entry, "capability", None)
    if capability is None:
        capability = getattr(agent, "capability", None)
    return capability


def check_rate_limit(
    registry: Any,
    agent: Any,
    rate_limiter: AgentRateLimiter | None,
    component: str,
) -> bool:
    """Check agent rate limit. Returns True if allowed, False if blocked.

    Resolves max_calls_per_hour from the agent's capability via the
    registry (falls back to DEFAULT_MAX_CALLS_PER_HOUR).
    """
    if rate_limiter is None:
        return True

    capability = get_capability(registry, agent)
    max_calls = DEFAULT_MAX_CALLS_PER_HOUR
    if capability is not None:
        max_calls = capability.max_calls_per_hour

    if not rate_limiter.check_and_record(agent.name, max_calls):
        logger.warning("[%s] Agent '%s' rate limited, skipping", component, agent.name)
        return False
    return True


async def timed_analyze(agent: Any, task: Any) -> Any:
    """Dispatch agent.analyze() with call duration logging."""
    start = time.monotonic()
    try:
        return await agent.analyze(task)
    finally:
        logger.info(
            "[Dispatch] Agent '%s' analyze took %.2fs",
            agent.name,
            time.monotonic() - start,
        )


async def _analyze_with_duration(agent: Any, task: Any) -> tuple[Any, float]:
    """timed_analyze plus the elapsed seconds (for dispatch-stats recording)."""
    start = time.monotonic()
    result = await timed_analyze(agent, task)
    return result, time.monotonic() - start


async def dispatch_with_gates(
    agents: list,
    task: Any,
    registry: Any,
    rate_limiter: AgentRateLimiter | None,
    component: str,
    baseline_tracker: Any = None,
) -> tuple[list[Any], int, int]:
    """Run analyze() on all agents in parallel with per-agent dispatch gates.

    Before each agent's analyze():
      1. Identity verification (suspended / tokenless-remote / invalid token
         agents are skipped, never fatal).
      2. Rate limit check against the agent's capability.
      3. Scope filtering of the task's ``context`` dict (agents with a
         capability declaring non-empty access_scopes see only those keys;
         ``agent_focus_areas`` is orchestrator metadata and always passes).

    After analyze(), findings citing data sources outside the agent's
    scopes are logged (analysis is kept -- flags are advisory).

    baseline_tracker: optional AgentBaselineTracker. When provided, each
    successful dispatch records stats (duration, refused = premise_valid
    is False, confidence attr when present, scope violation count) --
    best-effort, never fatal. Callers that don't pass it are unaffected.

    Returns (analyses, skipped_count, failed_count) where skipped_count is
    agents blocked by a gate and failed_count is agents whose analyze()
    raised. Works with registry=None (all gates pass; no filtering by
    registry capability).
    """
    scope_filter = ScopeFilter()
    coros = []
    dispatched: list[Any] = []
    capabilities: list[AgentCapability | None] = []
    skipped = 0
    for agent in agents:
        if not verify_agent_identity(agent, registry, component):
            skipped += 1
            continue
        if not check_rate_limit(registry, agent, rate_limiter, component):
            skipped += 1
            continue
        if registry is not None and hasattr(registry, "touch_last_active"):
            registry.touch_last_active(agent.name)

        capability = get_capability(registry, agent)
        agent_task = task
        context = getattr(task, "context", None)
        if isinstance(context, dict):
            filtered = scope_filter.filter_data(context, capability)
            if "agent_focus_areas" in context and "agent_focus_areas" not in filtered:
                filtered = {**filtered, "agent_focus_areas": context["agent_focus_areas"]}
            agent_task = replace(task, context=filtered)

        dispatched.append(agent)
        capabilities.append(capability)
        coros.append(_analyze_with_duration(agent, agent_task))

    results = await asyncio.gather(*coros, return_exceptions=True)
    analyses = []
    failed = 0
    for i, item in enumerate(results):
        if isinstance(item, Exception):
            logger.error("[%s] %s failed: %s", component, dispatched[i].name, item)
            failed += 1
            continue
        r, duration = item
        capability = capabilities[i]
        violation_count = 0
        observations = getattr(r, "observations", None)
        if capability is not None and isinstance(observations, list):
            violations = scope_filter.check_output_sources(observations, capability)
            violation_count = len(violations)
            if violations:
                logger.warning(
                    "[%s] Agent '%s': %d scope violation(s) in output: %s",
                    component,
                    dispatched[i].name,
                    len(violations),
                    "; ".join(violations),
                )
        if baseline_tracker is not None:
            try:
                baseline_tracker.record_dispatch(
                    agent_id=dispatched[i].name,
                    duration_seconds=duration,
                    refused=getattr(r, "premise_valid", True) is False,
                    confidence=float(getattr(r, "confidence", 0.0) or 0.0),
                    scope_violations=violation_count,
                )
            except Exception as exc:
                logger.warning(
                    "[%s] baseline stats recording failed (ignored): %s",
                    component,
                    exc,
                )
        analyses.append(r)
    return analyses, skipped, failed
