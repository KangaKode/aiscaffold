"""
Mock LLM client -- deterministic, offline, zero-cost.

Drop-in for LLMClient in tests, demos, and load runs: it returns canned
responses keyed by call role, so the full orchestration path (parsing,
enforcement, artifact writing, routing) exercises end-to-end without an
API key or network. Every canned response is written to pass the
enforcement pipeline (no speculation/opinion/confidence patterns; evidence
tags in [VERIFIED: ...] form).

Two ways to use it:
  - Role-keyed defaults: MockLLMClient() answers each role with a sensible
    canned payload (analysis JSON for *_analysis, synthesis JSON for
    "synthesis", a clean prose answer for chat/specialist, etc.).
  - Scripted: MockLLMClient(script=["first", "second", ...]) returns those
    strings in order (then repeats the last), for tests that need exact,
    deterministic content regardless of role.

This is duck-typed against LLMClient (call / total_usage / provider /
model), not a subclass, so constructing it never touches a provider SDK.
"""

import json
import logging
import time
from typing import Any

from .client import CacheablePrompt, LLMResponse, TokenUsage

logger = logging.getLogger(__name__)

_STRATEGY_RESPONSE = json.dumps(
    {
        "task_decomposition": ["Identify inputs", "Analyze", "Recommend"],
        "agent_focus_areas": {},
        "anticipated_tensions": ["Speed versus thoroughness"],
        "success_criteria": ["Findings cite evidence", "Recommendations are actionable"],
    }
)

_ANALYSIS_RESPONSE = json.dumps(
    {
        "observations": [
            {
                "finding": "The dataset contains 14 authentication failures from one source in five minutes",
                "evidence": "[VERIFIED: auth_logs:row_2847] Timestamped failure entries",
                "severity": "high",
                "confidence": 0.9,
            }
        ],
        "recommendations": [
            {
                "action": "Add progressive rate limiting to the authentication endpoint",
                "rationale": "Bounds brute-force attempts without blocking valid retries",
                "priority": "high",
            }
        ],
        "premise_valid": True,
        "refusal_reason": "",
    }
)

_CHALLENGE_RESPONSE = json.dumps(
    {
        "challenges": [
            {
                "target_agent": "evidence_analyst",
                "finding_challenged": "Pattern matches an attack signature",
                "counter_evidence": "[INDICATED: behavioral_data] Also matches monitoring tools",
            }
        ],
        "concessions": [],
    }
)

_SYNTHESIS_RESPONSE = json.dumps(
    {
        "recommended_direction": "Harden authentication with layered rate limiting and monitoring",
        "key_findings": [
            {
                "agent_name": "evidence_analyst",
                "finding": "Authentication failures concentrated in a five-minute window",
                "evidence": "[VERIFIED: auth_logs:row_2847] Direct log correlation",
            }
        ],
        "trade_offs": ["Rate limiting bounds attacks but can affect automated retries"],
        "minority_views": [],
    }
)

_VOTE_RESPONSE = json.dumps(
    {"approve": True, "conditions": [], "dissent_reason": ""}
)

_CROSS_CHECK_RESPONSE = json.dumps(
    {
        "agreement_level": 0.85,
        "consensus_points": ["Authentication hardening is the right response"],
        "conflicts": [],
    }
)

# Clean prose answer -- passes FactChecker (no speculation/opinion/hedging).
_CHAT_RESPONSE = (
    "The authentication logs record 14 failed attempts from a single source "
    "within a five-minute window. [VERIFIED: auth_logs:row_2847] Recommended "
    "actions: add progressive rate limiting and IP reputation scoring to the "
    "authentication pipeline."
)

_SENTINEL_RESPONSE = json.dumps(
    {
        "risk_level": "SAFE",
        "reasoning": "No extraction or manipulation indicators found",
        "indicators": [],
        "recommended_action": "proceed",
        "premise_valid": True,
        "refusal_reason": "",
        "approve": True,
        "challenges": [],
    }
)

_ROLE_RESPONSES: dict[str, str] = {
    "synthesis": _SYNTHESIS_RESPONSE,
    "cross_check": _CROSS_CHECK_RESPONSE,
    "chat_synthesis": _CHAT_RESPONSE,
    "enforcement_rewrite": _CHAT_RESPONSE,
    "specialist": _CHAT_RESPONSE,
    "analyst": _ANALYSIS_RESPONSE,
    "resolve": _CHAT_RESPONSE,
}

_ROLE_SUFFIX_MAP: dict[str, str] = {
    "analysis": _ANALYSIS_RESPONSE,
    "challenge": _CHALLENGE_RESPONSE,
    "vote": _VOTE_RESPONSE,
}


def _select_response(role: str) -> str:
    """Pick a canned response for a call role (exact, then suffix, then default)."""
    if role.startswith("sentinel"):
        return _SENTINEL_RESPONSE
    if role in _ROLE_RESPONSES:
        return _ROLE_RESPONSES[role]
    for suffix, response in _ROLE_SUFFIX_MAP.items():
        if role.endswith(f"_{suffix}"):
            return response
    if "strategy" in role:
        return _STRATEGY_RESPONSE
    return _ANALYSIS_RESPONSE


class MockLLMClient:
    """Deterministic offline LLM client (duck-typed against LLMClient).

    Args:
        script: optional list of response strings returned in order; the
            last entry repeats once exhausted. When set, script takes
            precedence over role-keyed defaults.
        response: optional single response string returned for every call.
    """

    def __init__(
        self,
        script: list[str] | None = None,
        response: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._script = list(script) if script else None
        self._fixed = response
        self._call_count = 0
        self.budget_manager = None

    async def call(
        self,
        prompt: str | CacheablePrompt,
        role: str = "assistant",
        temperature: float = 0.5,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        self._call_count += 1
        start = time.time()

        if self._fixed is not None:
            content = self._fixed
        elif self._script is not None:
            idx = min(self._call_count - 1, len(self._script) - 1)
            content = self._script[idx]
        else:
            content = _select_response(role)

        return LLMResponse(
            content=content,
            usage=TokenUsage(
                input_tokens=150,
                output_tokens=200,
                cached_input_tokens=100,
                estimated_cost_usd=0.0,
                cache_hit=True,
            ),
            model="mock-llm",
            provider="mock",
            latency_ms=(time.time() - start) * 1000,
            cached=True,
        )

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def total_usage(self) -> TokenUsage:
        return TokenUsage(
            input_tokens=150 * self._call_count,
            output_tokens=200 * self._call_count,
            cached_input_tokens=100 * self._call_count,
            estimated_cost_usd=0.0,
            cache_hit=True,
        )

    @property
    def provider(self) -> str:
        return "mock"

    @property
    def model(self) -> str:
        return "mock-llm"
