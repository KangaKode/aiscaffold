"""Fail-closed LLM response handling for core safety agents.

Core agents must never treat an LLM *transport* failure (budget
exhausted, client not initialized, call failed after retries) as a
benign non-answer: a screening agent that cannot screen has to refuse,
and a voting agent that cannot evaluate has to dissent.

Two failure modes are deliberately distinct:
  - LLM failure (``response.is_error``, or a known client error string
    from a duck-typed client that predates the contract): the call never
    produced model output -- callers fail CLOSED.
  - Parse failure on a real response: the model answered but with
    malformed JSON -- callers degrade gracefully (a live model's
    formatting quirk is not a security event).
"""

from typing import Any

from ...llm.json_parser import extract_json

# Error prefixes emitted by LLMClient before the is_error contract
# existed; checked so duck-typed clients that copy those strings still
# fail closed.
_CLIENT_ERROR_PREFIXES = (
    "[LLM call failed",
    "[Budget exhausted",
    "[LLM client not initialized",
)


def llm_call_failed(response: Any) -> bool:
    """True when the LLM call itself failed (no real model output)."""
    if bool(getattr(response, "is_error", False)):
        return True
    content = getattr(response, "content", "") or ""
    return content.startswith(_CLIENT_ERROR_PREFIXES)


def parse_agent_json(response: Any) -> dict | None:
    """Extract a JSON dict from a live LLM response.

    Returns None when the call failed (see ``llm_call_failed``) or when
    the content holds no parseable JSON object. Callers that need to act
    differently on the two cases disambiguate with ``llm_call_failed``.
    """
    if llm_call_failed(response):
        return None
    data = extract_json(getattr(response, "content", "") or "")
    return data if isinstance(data, dict) else None
