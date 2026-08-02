"""Helper functions for the RoundTable orchestrator.

Extracted from round_table.py to keep that file under 520 lines.

Keep this file under 550 lines.
"""

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .round_table import (
        AgentAnalysis,
        RoundTableConfig,
        RoundTableResult,
        RoundTableTask,
        StrategyPlan,
    )

logger = logging.getLogger(__name__)

SENTINEL_ENFORCEMENT_ENV = "SENTINEL_ENFORCEMENT_ENABLED"


def resolve_sentinel_enforcement(configured: bool | None) -> bool:
    """Resolve the Sentinel enforcement setting once, at RoundTable init.

    Explicit config (True/False) wins; None falls back to the
    SENTINEL_ENFORCEMENT_ENABLED env flag with the same truthy parse as
    the other opt-in toggles (see llm.model_router.create_model_router).
    Default is OFF: Sentinel stays advisory (detect, never act).
    """
    if configured is not None:
        return configured
    return os.environ.get(SENTINEL_ENFORCEMENT_ENV, "").strip().lower() in (
        "true", "1", "yes",
    )


def init_sentinel_enforcement(
    config: "RoundTableConfig", sentinel_agent: Any
) -> tuple[bool, frozenset]:
    """Resolve the enforcement state once, at RoundTable init.

    sentinel_agent is the core roster's Sentinel OBJECT (None when the
    roster is off or failed to load). Enforcement binds to that exact
    object -- its analysis is captured at dispatch by object identity
    (see dispatch_helpers.dispatch_with_gates capture_sink), so neither
    an agent merely named "sentinel" nor an analysis whose agent_name
    lies can satisfy it or earn the rate-limit exemption.
    Returns (enabled, rate_limit_exempt).
    """
    enabled = resolve_sentinel_enforcement(config.sentinel_enforcement)
    if enabled and sentinel_agent is None:
        logger.warning(
            "[RoundTable] Sentinel enforcement is ON but the core Sentinel "
            "is not on the roster -- every run will refuse with "
            "sentinel_missing"
        )
    exempt = (
        frozenset({sentinel_agent.name})
        if enabled and sentinel_agent is not None
        else frozenset()
    )
    return enabled, exempt


@dataclass
class SentinelRefusal:
    """Machine-readable refusal contract for the opt-in short-circuit.

    reason is a bounded enum:
      sentinel_high_risk   -- screening verdict risk_level == "HIGH"
      sentinel_unavailable -- Sentinel could not screen (no LLM /
                              transport or budget failure); fail-closed
      sentinel_premise     -- Sentinel asserted premise_valid=False for
                              any other reason
      sentinel_missing     -- enforcement is on but no core Sentinel
                              analysis was produced in Phase 1 (roster
                              off, core load failure, dispatch gate,
                              analyze() crash)

    detail and observation flow into local artifacts only; the API
    response surfaces just the bounded reason enum. Surfaced strings are
    length-capped and null-stripped (sanitize_for_prompt), NOT
    injection-neutralized -- treat artifact contents as untrusted.
    """

    reason: str
    detail: str = ""
    observation: dict = field(default_factory=dict)


def _sanitize_sentinel_observation(obs: dict) -> dict:
    """Sanitize the captured screening observation for surfacing.

    Screening evidence quotes untrusted task content, so every surfaced
    string passes sanitize_for_prompt with a length cap (indicators are
    sanitized per element), per the premise-gate precedent.
    """
    from ..security.prompt_guard import sanitize_for_prompt

    sanitized = {
        "finding": sanitize_for_prompt(str(obs.get("finding", "")), max_length=200),
        "evidence": sanitize_for_prompt(str(obs.get("evidence", "")), max_length=500),
        "risk_level": str(obs.get("risk_level", ""))[:16],
    }
    indicators = obs.get("indicators")
    if isinstance(indicators, list):
        sanitized["indicators"] = [
            sanitize_for_prompt(str(item), max_length=200)
            for item in indicators[:10]
        ]
    return sanitized


def check_sentinel_refusal(
    sentinel_analysis: "AgentAnalysis | None",
) -> SentinelRefusal | None:
    """Map the captured core-Sentinel analysis to a refusal, or None.

    The caller captures the analysis BEFORE the evidence-enforcement
    pipeline runs (pre-pipeline capture), so pipeline drops or rewrites
    can never erase the trigger. A parse-failure analysis (premise_valid
    True, no HIGH observation) never halts -- malformed JSON from a live
    model is a formatting quirk, not a screening verdict.
    """
    from ..security.prompt_guard import sanitize_for_prompt

    if sentinel_analysis is None:
        return SentinelRefusal(
            reason="sentinel_missing",
            detail="No core Sentinel analysis was produced in Phase 1",
        )
    observations = getattr(sentinel_analysis, "observations", None) or []
    first_obs = (
        observations[0]
        if observations and isinstance(observations[0], dict)
        else {}
    )
    if getattr(sentinel_analysis, "premise_valid", True) is False:
        raw_reason = getattr(sentinel_analysis, "refusal_reason", "") or ""
        return SentinelRefusal(
            reason=(
                "sentinel_unavailable"
                if raw_reason == "sentinel_unavailable"
                else "sentinel_premise"
            ),
            detail=sanitize_for_prompt(raw_reason, max_length=200),
            observation=_sanitize_sentinel_observation(first_obs),
        )
    for obs in observations:
        if isinstance(obs, dict) and obs.get("risk_level") == "HIGH":
            return SentinelRefusal(
                reason="sentinel_high_risk",
                detail=sanitize_for_prompt(
                    str(obs.get("evidence", "")), max_length=200
                ),
                observation=_sanitize_sentinel_observation(obs),
            )
    return None


def finalize_sentinel_refusal(
    result: "RoundTableResult",
    sentinel_analysis: "AgentAnalysis | None",
    write_artifact,
) -> bool:
    """Apply the enforcement decision to the result. Returns True when
    the run must short-circuit (result.sentinel_refusal is set and the
    phase1_sentinel_refusal artifact is written)."""
    refusal = check_sentinel_refusal(sentinel_analysis)
    if refusal is None:
        return False
    result.sentinel_refusal = refusal
    logger.warning(
        f"[RoundTable] Task {result.task_id}: Sentinel enforcement refused "
        f"the run ({refusal.reason}) -- short-circuiting before Phase 2"
    )
    write_artifact(result.task_id, "phase1_sentinel_refusal", asdict(refusal))
    return True


def build_system_prompt(agents: list) -> str:
    """The orchestrator's stable system prompt (cached across calls)."""
    agent_info = ", ".join(f"{a.name} ({a.domain})" for a in agents)
    return (
        f"You are an orchestrator coordinating {len(agents)} specialist agents: "
        f"{agent_info}.\n\n"
        f"Rules:\n"
        f"- Preserve ALL evidence fields from agent outputs\n"
        f"- Do NOT summarize away supporting quotes, data, or citations\n"
        f"- Surface disagreements -- minority views are valuable\n"
        f"- Return valid JSON"
    )


def write_artifact(
    config: "RoundTableConfig", task_id: str, phase: str, data: Any
) -> None:
    """Write intermediate results to the filesystem for auditability."""
    if not config.write_artifacts:
        return
    artifact_dir = config.artifacts_dir / task_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{phase}.json"
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        logger.debug(f"[RoundTable] Artifact: {path}")
    except Exception as e:
        logger.warning(f"[RoundTable] Artifact write failed: {e}")


async def phase_strategy(
    task: "RoundTableTask",
    llm: object,
    system_prompt: str,
    agents: list,
) -> "StrategyPlan":
    """Phase 0: Orchestrator plans before dispatching."""
    from ..llm import CacheablePrompt
    from .round_table import StrategyPlan

    prompt = CacheablePrompt(
        system=system_prompt,
        user_message=(
            f"Task: {task.content}\n\n"
            f"Before dispatching the team, plan your strategy:\n"
            f"1. How does this task decompose into sub-problems?\n"
            f"2. What should each agent specifically focus on?\n"
            f"3. What disagreements do you anticipate between agents?\n"
            f"4. What are the success criteria?\n\n"
            'Return JSON: {"task_decomposition": [...], "agent_focus_areas": {...}, '
            '"anticipated_tensions": [...], "success_criteria": [...]}'
        ),
    )
    try:
        from ..llm.json_parser import extract_json

        response = await llm.call(prompt=prompt, role="synthesis", temperature=0.3)
        data = extract_json(response.content)
        if data is None:
            logger.warning("[RoundTable] Strategy phase returned unparseable JSON")
            return StrategyPlan(reasoning=response.content)
        return StrategyPlan(
            task_decomposition=data.get("task_decomposition", []),
            agent_focus_areas=data.get("agent_focus_areas", {}),
            anticipated_tensions=data.get("anticipated_tensions", []),
            success_criteria=data.get("success_criteria", []),
            reasoning=response.content,
        )
    except Exception as e:
        logger.warning(f"[RoundTable] Strategy phase failed: {e}")
        return StrategyPlan(
            task_decomposition=["Full analysis"],
            agent_focus_areas={a.name: a.domain for a in agents},
            success_criteria=["Actionable recommendations with evidence"],
        )


async def phase_synthesis(
    partial: "RoundTableResult",
    llm: object,
    system_prompt: str,
    learning_store: object = None,
    tenant_id: str = "default",
) -> tuple:
    """Phase 3a: Synthesize analyses. CRITICAL: preserve ALL evidence fields.

    Returns (synthesis, canary_refusal). canary_refusal is set only when
    runtime canary enforcement trips on a leaked token.
    """
    import asyncio

    from ..llm import CacheablePrompt
    from .round_table import SynthesisResult
    from .runtime_canary import (
        CanaryRefusal,
        SURFACE_ROUND_TABLE,
        canary_context_section,
        observe_response,
        should_refuse,
    )

    if not llm:
        return (
            SynthesisResult(recommended_direction="No LLM available for synthesis"),
            None,
        )

    try:
        analyses_json = json.dumps(
            [{"agent": a.agent_name, "domain": a.domain,
              "observations": a.observations, "recommendations": a.recommendations,
              "confidence": a.confidence} for a in partial.analyses],
            indent=2, default=str,
        )
    except Exception as e:
        logger.warning(f"[RoundTable] Analysis serialization failed: {e}")
        analyses_json = json.dumps(
            [{"agent": a.agent_name, "domain": a.domain}
             for a in partial.analyses], indent=2,
        )

    analyses_section, canary = canary_context_section(analyses_json)
    prompt = CacheablePrompt(
        system=system_prompt,
        context=(
            f"Analyses from {len(partial.analyses)} agents:\n{analyses_section}"
        ),
        user_message=(
            "Synthesize these specialist analyses into a recommendation.\n\n"
            'Return JSON: {"recommended_direction": "...", '
            '"key_findings": [{"agent_name": ..., "finding": ..., "evidence": ...}], '
            '"trade_offs": [...], "minority_views": [...]}'
        ),
    )
    try:
        from ..llm.json_parser import extract_json
        from ..llm.response_guard import llm_call_failed

        response = await llm.call(prompt=prompt, role="synthesis", temperature=0.2)

        if not response or not response.content:
            logger.warning("[RoundTable] Synthesis returned empty response")
            return (
                SynthesisResult(
                    recommended_direction="Synthesis returned empty response"
                ),
                None,
            )

        if llm_call_failed(response):
            # Never surface the client's error string as the recommended
            # direction -- it flows into check-ins and API responses.
            logger.warning("[RoundTable] Synthesis LLM call failed -- using fallback")
            return (
                SynthesisResult(
                    recommended_direction=(
                        "Synthesis failed -- review individual analyses"
                    )
                ),
                None,
            )

        leaked = await asyncio.to_thread(
            observe_response,
            response.content,
            canary,
            store=learning_store,
            tenant_id=tenant_id,
            surface=SURFACE_ROUND_TABLE,
        )
        if should_refuse(leaked):
            return SynthesisResult(recommended_direction=""), CanaryRefusal()

        data = extract_json(response.content)
        if data is None:
            logger.warning("[RoundTable] Synthesis returned unparseable JSON")
            return (
                SynthesisResult(recommended_direction=response.content[:500]),
                None,
            )

        if not isinstance(data, dict):
            logger.warning("[RoundTable] Synthesis returned non-dict JSON")
            return (
                SynthesisResult(recommended_direction=str(data)[:500]),
                None,
            )

        return (
            SynthesisResult(
                recommended_direction=data.get("recommended_direction", ""),
                key_findings=data.get("key_findings", []),
                trade_offs=data.get("trade_offs", []),
                minority_views=data.get("minority_views", []),
            ),
            None,
        )
    except Exception as e:
        logger.warning(f"[RoundTable] Synthesis failed: {e}")
        return (
            SynthesisResult(
                recommended_direction="Synthesis failed -- review individual analyses"
            ),
            None,
        )


def apply_approval_gate(
    result: "RoundTableResult",
    config: "RoundTableConfig",
    checkin_manager: object = None,
) -> None:
    """Mark the result as requiring human approval when the config or the
    resolved autonomy policy demands it, and create a check-in if a
    manager is available.

    Mutates result.requires_approval in place. Check-in creation is
    best-effort -- a storage failure never fails the round table.
    """
    needs_approval = config.require_human_approval
    if config.autonomy_level is not None:
        from .autonomy import resolve_policy

        policy = resolve_policy(config.autonomy_level)
        needs_approval = needs_approval or policy.require_human_approval

    if not needs_approval:
        return

    result.requires_approval = True
    logger.info(
        f"[RoundTable] Task {result.task_id}: human approval required "
        f"before acting on this result"
    )

    if checkin_manager is None:
        return
    try:
        direction = ""
        if result.synthesis is not None:
            direction = result.synthesis.recommended_direction[:500]
        checkin_manager.create(
            checkin_type="approval",
            prompt=(
                f"Round table task '{result.task_id}' finished "
                f"(consensus: {'yes' if result.consensus_reached else 'no'}, "
                f"approval rate: {result.approval_rate:.0%}) and requires "
                f"your approval before its recommendation is acted on."
            ),
            suggested_action=direction or "Review the round table result",
            context={"task_id": result.task_id},
        )
    except Exception as e:
        logger.warning(
            f"[RoundTable] Could not create approval check-in: {type(e).__name__}"
        )


async def record_collusion_votes(
    detector: object,
    task: "RoundTableTask",
    votes: list,
) -> None:
    """Feed one round of votes into the collusion detector (opt-in hook).

    Detect-only and fire-and-forget: findings persist as integrity flags
    inside the detector; any failure (detector bug, broken store) is
    logged and swallowed so vote recording can never fail a deliberation.
    The store I/O is blocking, so the call runs off the event loop.
    """
    try:
        await asyncio.to_thread(
            detector.record_votes,
            task.id,
            votes,
            getattr(task, "tenant_id", "default") or "default",
        )
    except Exception as e:
        logger.warning(
            f"[RoundTable] Collusion vote recording failed (ignored): {e}"
        )


async def enforce_evidence(
    analyses: list["AgentAnalysis"],
    task: "RoundTableTask",
    llm: object,
) -> list["AgentAnalysis"]:
    """Run the evidence enforcement pipeline on each analysis.

    Rejected analyses are dropped; corrected observation lists replace the
    originals. Any pipeline failure falls back to the unmodified analyses.
    """
    try:
        from ..enforcement import EvidenceEnforcementPipeline
        from ..llm.json_parser import extract_json
        from .round_table import AgentAnalysis

        pipeline = EvidenceEnforcementPipeline(llm_client=llm)
        enforced = []
        for analysis in analyses:
            text = json.dumps(analysis.observations, default=str)
            result = await pipeline.validate(analysis.agent_name, text, task)
            if result.violations:
                logger.info(
                    f"[RoundTable] {analysis.agent_name}: "
                    f"{len(result.violations)} enforcement violations "
                    f"({result.outcome})"
                )
            if result.outcome == "rejected":
                logger.warning(
                    f"[RoundTable] Dropping rejected analysis from {analysis.agent_name}"
                )
                continue
            if result.corrected_content:
                try:
                    corrected_data = extract_json(result.corrected_content)
                except Exception:
                    corrected_data = None
                if isinstance(corrected_data, list):
                    analysis = AgentAnalysis(
                        agent_name=analysis.agent_name,
                        domain=analysis.domain,
                        observations=corrected_data,
                        recommendations=analysis.recommendations,
                        confidence=analysis.confidence,
                        raw_response=analysis.raw_response,
                    )
                elif result.outcome != "accepted":
                    logger.warning(
                        f"[RoundTable] Dropping {analysis.agent_name}: "
                        f"corrected analysis was not parseable"
                    )
                    continue
            enforced.append(analysis)
        return enforced
    except Exception as e:
        logger.warning(f"[RoundTable] Evidence enforcement failed: {e}")
        return analyses
