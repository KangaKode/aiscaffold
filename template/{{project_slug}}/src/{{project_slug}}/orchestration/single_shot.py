"""
Single-shot resolution -- Tier 1 of a three-tier resolution architecture.

Tiers, cheapest first:
  1. Single-shot (this module): one cheap LLM call grounded in approved
     corrections, gated by mandatory enforcement.
  2. Chat (chat_orchestrator.py): interactive multi-agent consultation.
  3. Round table (round_table.py): full adversarial deliberation.

Single-shot answers routine, well-understood queries fast and cheap by
reusing what the platform already learned (approved corrections). It is a
routing optimization, NOT a quality bypass: the same FactChecker
enforcement that guards chat runs here too, and a confidence gate escalates
to chat whenever the cheap answer is not trustworthy (enforcement rejected,
no evidence citations, or the model itself says the knowledge is
insufficient). User content is always wrapped before it reaches the model.

The LLM call uses role "single_shot_resolution", which the model router
(llm/model_router.py) maps to the nano tier -- so this path is both one
call and the cheapest model.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..enforcement.fact_checker import FactChecker
from ..llm.client import CacheablePrompt
from ..security.prompt_guard import wrap_user_content
from .ingest_scan import scan_user_message

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 2048

SINGLE_SHOT_SYSTEM_PROMPT = (
    "You are a knowledge resolver. Answer the query using ONLY the "
    "institutional knowledge provided (approved corrections from prior "
    "analyses). \n\n"
    "For every claim, cite the supporting knowledge with an evidence tag: "
    "[VERIFIED: source], [CORROBORATED: src1 + src2], [INDICATED: source], "
    "or [POSSIBLE]. Do not speculate, hedge, give opinions, or state numeric "
    "confidence. If the provided knowledge is insufficient to answer, say so "
    "explicitly and use [INSUFFICIENT] -- do not guess."
)

_EVIDENCE_PATTERN = re.compile(
    r"\[(VERIFIED|CORROBORATED|INDICATED|POSSIBLE|INSUFFICIENT)[^\]]*\]"
)


@dataclass
class SingleShotResult:
    """Outcome of a single-shot resolution attempt.

    When escalated is True, content is empty and escalation_reason explains
    why the caller should fall back to POST /api/v1/chat.
    """

    content: str = ""
    tier: str = "single_shot"
    enforcement_result: str = ""
    enforcement_violations: list[str] = field(default_factory=list)
    evidence_level: str = ""
    citations_count: int = 0
    escalated: bool = False
    escalation_reason: str = ""
    duration_seconds: float = 0.0


def _primary_evidence_level(text: str) -> str:
    levels = _EVIDENCE_PATTERN.findall(text)
    for level in ("VERIFIED", "CORROBORATED", "INDICATED", "POSSIBLE", "INSUFFICIENT"):
        if level in levels:
            return level
    return ""


def _count_evidence_tags(text: str) -> int:
    return len(_EVIDENCE_PATTERN.findall(text))


def _escalate(reason: str, start: float, **extra) -> SingleShotResult:
    logger.info("[SingleShot] Escalating to chat: %s", reason)
    return SingleShotResult(
        escalated=True,
        escalation_reason=f"{reason}. Use POST /api/v1/chat for interactive analysis.",
        duration_seconds=time.monotonic() - start,
        **extra,
    )


async def resolve_single_shot(
    query: str,
    llm: Any,
    corrections_manager: Any = None,
    learning_store: Any = None,
    tenant_id: str = "default",
    agent_id: str = "",
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> SingleShotResult:
    """
    Resolve a query in one cheap LLM call, or escalate to chat.

    Requires an LLM client. corrections_manager (optional) supplies the
    approved-corrections context, and learning_store (optional) adds
    extracted error schemas (learning/error_schemata.py) alongside them.
    Without any learned knowledge the query escalates immediately --
    single-shot only answers what the platform has already learned.
    """
    start = time.monotonic()

    # Detect-only Layer 1 scan on the user query (logs + integrity flag;
    # never blocks, never mutates -- see orchestration/ingest_scan.py).
    # Runs before any escalation branch so every query gets scanned;
    # off-loop because the flag write is blocking store I/O.
    await asyncio.to_thread(
        scan_user_message, query,
        surface="resolve", store=learning_store, tenant_id=tenant_id,
    )

    if llm is None:
        return _escalate("No LLM client configured", start)

    from ..learning.knowledge_context import build_knowledge_context

    context = build_knowledge_context(
        corrections_manager=corrections_manager,
        learning_store=learning_store,
        tenant_id=tenant_id,
        agent_id=agent_id,
    )

    if not context:
        return _escalate("No approved corrections to ground an answer", start)

    prompt = CacheablePrompt(
        system=SINGLE_SHOT_SYSTEM_PROMPT,
        context=wrap_user_content(context, label="INSTITUTIONAL_KNOWLEDGE"),
        user_message=wrap_user_content(query, label="TASK_CONTENT"),
    )

    try:
        response = await llm.call(
            prompt=prompt,
            role="single_shot_resolution",
            temperature=0.1,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        logger.warning("[SingleShot] LLM call failed: %s", exc)
        return _escalate("Single-shot LLM call failed", start)

    text = response.content if response else ""

    # Enforcement is mandatory -- same gate as chat.
    checker = FactChecker()
    try:
        enforcement = checker.check(text)
        outcome = enforcement.outcome
        violations = [
            f"[{v.severity}] {v.rule}: {v.message}" for v in enforcement.violations
        ]
    except Exception:
        logger.warning("[SingleShot] FactChecker raised; escalating", exc_info=True)
        return _escalate("Enforcement error", start)

    evidence_level = _primary_evidence_level(text)
    citations = _count_evidence_tags(text)

    # Confidence gate: escalate on any weak signal.
    reasons = []
    if outcome == "rejected":
        reasons.append("enforcement_rejected")
    if citations == 0:
        reasons.append("no_citations")
    if "INSUFFICIENT" in text:
        reasons.append("insufficient_evidence")

    if reasons:
        return _escalate(
            f"Confidence gate failed: {', '.join(reasons)}",
            start,
            enforcement_result=outcome,
            enforcement_violations=violations,
            evidence_level=evidence_level,
            citations_count=citations,
        )

    return SingleShotResult(
        content=text,
        enforcement_result=outcome,
        enforcement_violations=violations,
        evidence_level=evidence_level,
        citations_count=citations,
        duration_seconds=time.monotonic() - start,
    )
