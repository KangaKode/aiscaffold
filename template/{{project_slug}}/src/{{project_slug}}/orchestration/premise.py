"""
Phase 0.5 -- Collective premise validation (the refusal gate).

Before committing to a full deliberation, every agent gets one cheap,
low-temperature LLM call asking a single question: is this task's premise
sound? When enough agents independently say no, the round table refuses
the task and returns what is wrong, what is missing, and a better
question -- instead of confidently analyzing a flawed premise.

Failure posture: this gate fails OPEN. A parse failure or LLM error for
one agent counts as "proceed" for that agent; if no LLM is configured the
gate is skipped entirely. The fail-closed backstop for hostile input is
the Sentinel's per-agent screening, not this gate.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .round_table import RoundTableTask

logger = logging.getLogger(__name__)

REFUSAL_REASONS = (
    "insufficient_data",
    "false_premise",
    "underspecified",
    "subjective",
    "out_of_scope",
)


@dataclass
class PremiseChallengeResult:
    """Outcome of the Phase 0.5 premise validation gate."""

    task_id: str
    premise_challenged: bool = False
    challenge_reasons: list[dict] = field(default_factory=list)
    # Each: {"agent_name": str, "refusal_reason": str, "better_question": str}
    what_is_wrong: str = ""
    what_is_missing: str = ""
    better_question: str = ""
    agents_who_would_proceed: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


def compute_refusal_threshold(n_agents: int, configured: int = 2) -> int:
    """Clamp the refusal threshold to a safe, meaningful range.

    A floor of 2 (for rooms of 2+) means a single compromised or
    misconfigured agent can never veto the whole round table by itself --
    tripping the gate requires independent agreement. The ceil(n/2) cap
    keeps the gate reachable in small rooms even when the configured
    threshold is larger than the room.
    """
    if n_agents <= 1:
        return 1
    return max(2, min(configured, -(-n_agents // 2)))


def _build_premise_prompt(agent_name: str, agent_domain: str) -> str:
    """Build the premise-check system prompt, personalized per agent.

    Including the agent's name and domain keeps verdicts genuinely
    diverse: a skeptic and a domain specialist should judge a premise
    from different angles, not return one shared answer five times.
    """
    return (
        f"You are {agent_name}, a specialist in {agent_domain}. Before the "
        "team commits to a full analysis, judge whether this task's premise "
        "is sound. This is a quick check, not the analysis itself.\n\n"
        "Answer:\n"
        "1. Is the premise sound? (premise_valid: true/false)\n"
        f"2. If not, why? (refusal_reason: one of {', '.join(REFUSAL_REASONS)})\n"
        "3. What information is missing that would make this answerable?\n"
        "4. How could the question be reframed to be answerable?\n\n"
        'Return JSON: {"premise_valid": bool, "refusal_reason": "...", '
        '"missing_info": "...", "better_question": "..."}'
    )


def _parse_premise_response(content: str, agent_name: str) -> dict:
    """Parse one agent's premise verdict. Fails OPEN (premise_valid=True)."""
    try:
        from ..llm.json_parser import extract_json

        data = extract_json(content)
        if isinstance(data, dict):
            return {
                "agent_name": agent_name,
                "premise_valid": bool(data.get("premise_valid", True)),
                "refusal_reason": str(data.get("refusal_reason", ""))[:200],
                "missing_info": str(data.get("missing_info", ""))[:500],
                "better_question": str(data.get("better_question", ""))[:500],
            }
    except Exception:
        logger.debug("[Phase0.5] Unparseable premise response from %s", agent_name)
    return {
        "agent_name": agent_name,
        "premise_valid": True,
        "refusal_reason": "",
        "missing_info": "",
        "better_question": "",
    }


def build_premise_challenge_result(
    task_id: str,
    agent_results: list[dict],
    duration: float,
    configured_threshold: int = 2,
) -> PremiseChallengeResult:
    """Deterministically synthesize per-agent verdicts into a gate outcome.

    Refusal fields are LLM output influenced by attacker-controlled task
    content, so everything surfaced to callers passes sanitize_for_prompt.
    The first non-empty better_question wins; missing_info is concatenated.
    """
    from ..security.prompt_guard import sanitize_for_prompt

    refusing = [r for r in agent_results if not r["premise_valid"]]
    proceeding = [r["agent_name"] for r in agent_results if r["premise_valid"]]
    threshold = compute_refusal_threshold(len(agent_results), configured_threshold)

    if len(refusing) < threshold:
        return PremiseChallengeResult(
            task_id=task_id,
            premise_challenged=False,
            agents_who_would_proceed=proceeding,
            duration_seconds=duration,
        )

    challenge_reasons = [
        {
            "agent_name": r["agent_name"],
            "refusal_reason": sanitize_for_prompt(r["refusal_reason"], max_length=200),
            "better_question": sanitize_for_prompt(r["better_question"], max_length=500),
        }
        for r in refusing
    ]

    reasons = "; ".join(
        f"{r['agent_name']}: {r['refusal_reason']}" for r in refusing if r["refusal_reason"]
    )
    missing = "; ".join(r["missing_info"] for r in refusing if r["missing_info"])
    better = next((r["better_question"] for r in refusing if r["better_question"]), "")

    return PremiseChallengeResult(
        task_id=task_id,
        premise_challenged=True,
        challenge_reasons=challenge_reasons,
        what_is_wrong=sanitize_for_prompt(reasons, max_length=1000),
        what_is_missing=sanitize_for_prompt(missing, max_length=1000),
        better_question=sanitize_for_prompt(better, max_length=500),
        agents_who_would_proceed=proceeding,
        duration_seconds=duration,
    )


async def phase_premise_validation(
    agents: list,
    task: "RoundTableTask",
    llm_client: Any,
    refusal_threshold: int = 2,
) -> PremiseChallengeResult:
    """Phase 0.5: parallel cheap premise checks before any expensive phase.

    Runs before Phase 0 strategy (nano-tier calls, no data dependency on
    the strategy plan) so a refused task costs almost nothing. Each
    agent's check that raises or fails to parse counts as "proceed".
    """
    from ..llm import CacheablePrompt
    from ..security.prompt_guard import wrap_user_content
    from .ingest_scan import scan_user_message

    start = time.monotonic()
    # Detect-only Layer 1 scan (log-only here: no store in scope, so no
    # blocking I/O -- safe to run inline). The gate's own outcome is
    # never influenced by the findings -- user content may legitimately
    # discuss injection techniques.
    scan_user_message(task.content, surface="premise_gate")
    wrapped_task = wrap_user_content(task.content, label="TASK_CONTENT")

    async def _check(agent: Any) -> dict:
        name = getattr(agent, "name", "unknown")
        domain = getattr(agent, "domain", "general analysis")
        try:
            prompt = CacheablePrompt(
                system=_build_premise_prompt(name, domain),
                user_message=f"Task: {wrapped_task}",
            )
            response = await llm_client.call(
                prompt=prompt, role="premise_validation", temperature=0.1
            )
            return _parse_premise_response(response.content, name)
        except Exception as e:
            logger.warning("[Phase0.5] Premise check failed for %s: %s", name, e)
            return {
                "agent_name": name,
                "premise_valid": True,
                "refusal_reason": "",
                "missing_info": "",
                "better_question": "",
            }

    results = await asyncio.gather(
        *[_check(a) for a in agents], return_exceptions=True
    )
    agent_results = [r for r in results if not isinstance(r, BaseException)]

    duration = time.monotonic() - start
    pcr = build_premise_challenge_result(
        task.id, agent_results, duration, configured_threshold=refusal_threshold
    )
    if pcr.premise_challenged:
        logger.info(
            "[Phase0.5] Premise challenged by %d agents (%.1fs) -- short-circuiting",
            len(pcr.challenge_reasons),
            duration,
        )
    else:
        logger.info("[Phase0.5] Premise valid, proceeding (%.1fs)", duration)
    return pcr
