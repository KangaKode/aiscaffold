"""
Chat helper functions extracted from chat_orchestrator.py.

Stateless enforcement utilities for the ChatOrchestrator.
Extracted to keep chat_orchestrator.py under 500 lines.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from ..enforcement.fact_checker import FactChecker

logger = logging.getLogger(__name__)

ENFORCEMENT_CORRECTION = (
    "Do not use speculation language, opinion phrases, or numeric "
    "confidence scores. Use evidence level tags instead: "
    "[VERIFIED: source:ref], [CORROBORATED: src1 + src2], "
    "[INDICATED: source], or [POSSIBLE]."
)


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
