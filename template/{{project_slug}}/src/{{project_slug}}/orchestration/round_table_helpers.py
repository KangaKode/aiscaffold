"""Helper functions for the RoundTable orchestrator.

Extracted from round_table.py to keep that file under 500 lines.
"""

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .round_table import AgentAnalysis, RoundTableTask

logger = logging.getLogger(__name__)


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
