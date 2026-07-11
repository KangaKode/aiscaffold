"""
Chat helper functions extracted from chat_orchestrator.py.

Stateless enforcement and cross-check utilities for the
ChatOrchestrator. Extracted to keep chat_orchestrator.py under 500 lines.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from ..enforcement.fact_checker import FactChecker
from ..llm import CacheablePrompt
from ..security.prompt_guard import wrap_user_content

logger = logging.getLogger(__name__)

ENFORCEMENT_CORRECTION = (
    "Do not use speculation language, opinion phrases, or numeric "
    "confidence scores. Use evidence level tags instead: "
    "[VERIFIED: source:ref], [CORROBORATED: src1 + src2], "
    "[INDICATED: source], or [POSSIBLE]."
)


async def cross_check_consultations(
    llm, consultations: list, escalation_threshold: float
):
    """Cross-check specialist responses for agreement/disagreement.

    Returns a CrossCheckResult (imported lazily to avoid a cycle with
    chat_orchestrator).
    """
    from .chat_orchestrator import CrossCheckResult

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

    response = await llm.call(prompt=prompt, role="cross_check", temperature=0.1)

    try:
        from ..llm.json_parser import extract_json

        data = extract_json(response.content)
        if data is None:
            return CrossCheckResult(agreement_level=0.5)
        agreement = float(data.get("agreement_level", 1.0))
        should_escalate = agreement < escalation_threshold

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


def _serialize_violations(result) -> list[str]:
    """Convert violations to API-safe strings (no internals)."""
    return [
        f"[{v.severity}] {v.rule}: {v.message}"
        for v in result.violations
    ]


async def enforce_chat_synthesis(
    content: str,
    re_synthesize: Callable[..., Awaitable[str]],
) -> tuple[str, str, list[str]]:
    """Run FactChecker on synthesis output. Retry once on rejection.

    FactChecker only for real-time chat (latency constraint);
    the full multi-checker pipeline is deferred to the Round Table.

    Args:
        content: The synthesis text to check.
        re_synthesize: Async callable accepting ``correction=str`` that
            produces a fresh synthesis when the first is rejected.

    Returns:
        (content, enforcement_result, enforcement_violations) where
        enforcement_result is "accepted", "challenged", or "rejected".
    """
    checker = FactChecker()
    try:
        result = checker.check(content)
    except Exception:
        logger.warning(
            "[ChatOrchestrator] FactChecker raised; accepting content",
            exc_info=True,
        )
        return content, "accepted", []

    violations = _serialize_violations(result)

    for v in result.violations:
        logger.debug(
            "[ChatOrchestrator] Enforcement violation (%s, %s): %s",
            v.rule,
            v.severity,
            v.message,
        )

    if result.outcome == "rejected":
        logger.info(
            "[ChatOrchestrator] Synthesis rejected (%d violations), retrying",
            len(result.violations),
        )
        content = await re_synthesize(
            correction=ENFORCEMENT_CORRECTION,
        )
        try:
            retry_result = checker.check(content)
        except Exception:
            logger.warning(
                "[ChatOrchestrator] FactChecker raised on retry",
                exc_info=True,
            )
            return content, "challenged", violations
        violations = _serialize_violations(retry_result)
        if retry_result.outcome == "rejected":
            logger.warning(
                "[ChatOrchestrator] Still rejected after retry (%d violations)",
                len(retry_result.violations),
            )
            return content, "rejected", violations
        return content, retry_result.outcome, violations

    return content, result.outcome, violations
