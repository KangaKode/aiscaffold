"""Helper functions for the RoundTable orchestrator.

Extracted from round_table.py to keep that file under 500 lines.
"""

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .round_table import (
        AgentAnalysis,
        RoundTableConfig,
        RoundTableResult,
        RoundTableTask,
        SynthesisResult,
    )

logger = logging.getLogger(__name__)


async def phase_synthesis(
    partial: "RoundTableResult",
    llm: object,
    system_prompt: str,
) -> "SynthesisResult":
    """Phase 3a: Synthesize analyses. CRITICAL: preserve ALL evidence fields."""
    from ..llm import CacheablePrompt
    from .round_table import SynthesisResult

    if not llm:
        return SynthesisResult(recommended_direction="No LLM available for synthesis")

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

    prompt = CacheablePrompt(
        system=system_prompt,
        context=(
            f"Analyses from {len(partial.analyses)} agents:\n{analyses_json}"
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

        response = await llm.call(prompt=prompt, role="synthesis", temperature=0.2)

        if not response or not response.content:
            logger.warning("[RoundTable] Synthesis returned empty response")
            return SynthesisResult(recommended_direction="Synthesis returned empty response")

        data = extract_json(response.content)
        if data is None:
            logger.warning("[RoundTable] Synthesis returned unparseable JSON")
            return SynthesisResult(recommended_direction=response.content[:500])

        if not isinstance(data, dict):
            logger.warning("[RoundTable] Synthesis returned non-dict JSON")
            return SynthesisResult(recommended_direction=str(data)[:500])

        return SynthesisResult(
            recommended_direction=data.get("recommended_direction", ""),
            key_findings=data.get("key_findings", []),
            trade_offs=data.get("trade_offs", []),
            minority_views=data.get("minority_views", []),
        )
    except Exception as e:
        logger.warning(f"[RoundTable] Synthesis failed: {e}")
        return SynthesisResult(recommended_direction="Synthesis failed -- review individual analyses")


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
