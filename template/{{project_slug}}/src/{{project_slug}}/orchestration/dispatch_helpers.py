"""Shared dispatch helpers for agent sandboxing.

Standalone functions used by orchestrators around each agent call:
identity verification, capability resolution, rate limiting, scope
filtering, and call duration logging -- plus the gated Phase 2/3
runners (run_challenge_phase, run_voting_phase) so every deliberation
phase passes the same gates.
Registry and rate limiter are passed as arguments (no globals).
"""

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any

from ..agents.capability import AgentCapability
from ..agents.rate_limiter import DEFAULT_MAX_CALLS_PER_HOUR, AgentRateLimiter
from .identity_check import resolve_registry_entry, verify_agent_identity
from .scope_filter import ScopeFilter

logger = logging.getLogger(__name__)


def get_capability(registry: Any, agent: Any) -> AgentCapability | None:
    """Resolve agent capability -- prefer registry entry over agent instance.

    The entry is resolved by agent OBJECT identity, so the capability
    binds to the exact entry being dispatched even when the same name
    exists in other tenants (a bare-name lookup would resolve to None
    on collision and silently disable scope filtering).
    """
    capability = None
    if registry:
        entry = resolve_registry_entry(registry, agent)
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


def gate_agents(
    agents: list,
    task: Any,
    registry: Any,
    rate_limiter: AgentRateLimiter | None,
    component: str,
) -> tuple[list[tuple[Any, Any, AgentCapability | None]], int]:
    """Run the per-agent dispatch gates and scope-filter the task.

    The single gate sequence shared by every deliberation phase
    (analyze, challenge, vote):
      1. Identity verification (suspended / tokenless-remote / invalid
         token agents are skipped, never fatal).
      2. Rate limit check against the agent's capability.
      3. Scope filtering of the task's ``context`` dict (agents with a
         capability declaring non-empty access_scopes see only those
         keys; ``agent_focus_areas`` is orchestrator metadata and
         always passes).

    Returns ([(agent, scope_filtered_task, capability), ...], skipped)
    where skipped counts agents blocked by a gate. Works with
    registry=None (all gates pass; no capability filtering).
    """
    scope_filter = ScopeFilter()
    gated: list[tuple[Any, Any, AgentCapability | None]] = []
    skipped = 0
    for agent in agents:
        if not verify_agent_identity(agent, registry, component):
            skipped += 1
            continue
        if not check_rate_limit(registry, agent, rate_limiter, component):
            skipped += 1
            continue
        if registry is not None and hasattr(registry, "touch_last_active"):
            # Scope the touch to the dispatched entry's tenant so a
            # same-name agent in another tenant is never the one updated.
            entry = resolve_registry_entry(registry, agent)
            registry.touch_last_active(
                agent.name, getattr(entry, "tenant_id", None)
            )

        capability = get_capability(registry, agent)
        agent_task = task
        context = getattr(task, "context", None)
        if isinstance(context, dict):
            filtered = scope_filter.filter_data(context, capability)
            if "agent_focus_areas" in context and "agent_focus_areas" not in filtered:
                filtered = {**filtered, "agent_focus_areas": context["agent_focus_areas"]}
            agent_task = replace(task, context=filtered)

        gated.append((agent, agent_task, capability))
    return gated, skipped


async def run_challenge_phase(
    agents: list,
    task: Any,
    analyses: list,
    registry: Any,
    component: str = "RoundTable",
) -> list:
    """Phase 2: agents challenge each other (mediated hub-and-spoke).

    Dispatch passes the same gates as Phase 1 (identity/suspension,
    rate limit, scope-filtered task) -- gates are re-checked, so an
    agent suspended mid-run is excluded here.
    """
    rate_limiter = getattr(registry, "rate_limiter", None)
    gated, skipped = gate_agents(agents, task, registry, rate_limiter, component)
    if skipped:
        logger.warning(
            "[%s] %d agent(s) blocked by dispatch gates at challenge phase",
            component, skipped,
        )
    results = await asyncio.gather(
        *[agent.challenge(agent_task, analyses) for agent, agent_task, _ in gated],
        return_exceptions=True,
    )
    challenges = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.error("[%s] %s challenge failed: %s", component, gated[i][0].name, r)
            continue
        challenges.append(r)
    return challenges


async def run_voting_phase(
    agents: list,
    task: Any,
    synthesis: Any,
    registry: Any,
    component: str = "RoundTable",
    midrun_names: set[str] | None = None,
) -> tuple[list, int]:
    """Phase 3b: agents vote on the synthesis. Dissent is valuable.

    Dispatch passes the same gates as Phase 1. Gated agents cast no
    vote and consensus divides by votes actually cast, so callers must
    mark the result degraded when the returned gated-out count is
    non-zero -- otherwise one surviving approver could present as 100%
    consensus. Note the asymmetry: a vote() that RAISES records a
    dissent, a gated-out voter is excluded from the denominator.

    midrun_names scopes the returned count to MID-RUN losses: when
    given (the round table passes the Phase 1 analysis contributors),
    only gated-out agents in that set are counted. Roster members that
    were already excluded before deliberation (e.g. suspended before
    the run) fail the same gates again here, and counting them would
    flag a clean consensus as degraded for a voter that never existed.

    Returns (votes, gated_out_count).
    """
    from .round_table import AgentVote  # deferred: avoids an import cycle

    rate_limiter = getattr(registry, "rate_limiter", None)
    gated, skipped = gate_agents(agents, task, registry, rate_limiter, component)
    if midrun_names is not None:
        gated_names = {agent.name for agent, _, _ in gated}
        skipped = sum(
            1 for agent in agents
            if agent.name not in gated_names and agent.name in midrun_names
        )
    if skipped:
        logger.warning(
            "[%s] %d mid-run agent(s) blocked by dispatch gates at vote phase",
            component, skipped,
        )
    results = await asyncio.gather(
        *[agent.vote(agent_task, synthesis) for agent, agent_task, _ in gated],
        return_exceptions=True,
    )
    votes = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.error("[%s] %s vote failed: %s", component, gated[i][0].name, r)
            votes.append(AgentVote(agent_name=gated[i][0].name, dissent_reason=str(r)))
            continue
        votes.append(r)
    return votes, skipped


async def dispatch_with_gates(
    agents: list,
    task: Any,
    registry: Any,
    rate_limiter: AgentRateLimiter | None,
    component: str,
    baseline_tracker: Any = None,
) -> tuple[list[Any], int, int]:
    """Run analyze() on all agents in parallel with per-agent dispatch gates.

    Each agent passes the shared gate sequence (see ``gate_agents``)
    before its analyze() is dispatched with the scope-filtered task.

    After analyze(), findings citing data sources outside the agent's
    scopes are logged (analysis is kept -- flags are advisory).

    baseline_tracker: optional AgentBaselineTracker. When provided, each
    successful dispatch records stats (duration, refused = premise_valid
    is False, confidence attr when present, scope violation count) under
    the task's tenant_id -- best-effort, never fatal, store write runs
    off the event loop. Callers that don't pass it are unaffected.

    Returns (analyses, skipped_count, failed_count) where skipped_count is
    agents blocked by a gate and failed_count is agents whose analyze()
    raised. Works with registry=None (all gates pass; no filtering by
    registry capability).
    """
    scope_filter = ScopeFilter()
    gated, skipped = gate_agents(agents, task, registry, rate_limiter, component)
    dispatched = [agent for agent, _, _ in gated]
    capabilities = [capability for _, _, capability in gated]
    coros = [
        _analyze_with_duration(agent, agent_task)
        for agent, agent_task, _ in gated
    ]

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
                # Blocking store write -- off the event loop. Tenant comes
                # from the task (RoundTableTask.tenant_id, threaded from
                # the caller's auth context by the API routes).
                await asyncio.to_thread(
                    baseline_tracker.record_dispatch,
                    agent_id=dispatched[i].name,
                    duration_seconds=duration,
                    refused=getattr(r, "premise_valid", True) is False,
                    confidence=float(getattr(r, "confidence", 0.0) or 0.0),
                    scope_violations=violation_count,
                    tenant_id=getattr(task, "tenant_id", "default") or "default",
                )
            except Exception as exc:
                logger.warning(
                    "[%s] baseline stats recording failed (ignored): %s",
                    component,
                    exc,
                )
        analyses.append(r)
    return analyses, skipped, failed
