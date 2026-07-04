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
    )

logger = logging.getLogger(__name__)


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
