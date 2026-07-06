"""Failure classification for LLM responses (fail-closed helpers).

Callers that consume LLM output for safety-relevant decisions must never
treat an LLM *transport* failure (budget exhausted, client not
initialized, call failed after retries) as a benign non-answer: a
screening agent that cannot screen has to refuse, a voting agent that
cannot evaluate has to dissent, and a rewrite that never happened must
not launder a rejected response.

Two failure modes are deliberately distinct:
  - LLM failure (see ``llm_call_failed``): the call never produced model
    output -- callers fail CLOSED.
  - Parse failure on a real response: the model answered but with
    malformed JSON -- callers degrade gracefully (a live model's
    formatting quirk is not a security event).
"""

from typing import Any

from .json_parser import extract_json

# Error prefixes emitted by LLMClient before the is_error contract
# existed; checked only for duck-typed clients that predate the contract
# (no is_error attribute at all).
_LEGACY_ERROR_PREFIXES = (
    "[LLM call failed",
    "[Budget exhausted",
    "[LLM client not initialized",
)

_MISSING = object()


def llm_call_failed(response: Any) -> bool:
    """True when the LLM call itself failed (no real model output).

    Responses implementing the shipped contract (an ``is_error``
    attribute, present on every ``LLMResponse``) are trusted
    exclusively -- content is never inspected, so a live model reply
    that merely QUOTES a bracketed error string is not misclassified.
    The legacy prefix check applies only to duck-typed clients whose
    responses lack the attribute entirely.
    """
    is_error = getattr(response, "is_error", _MISSING)
    if is_error is not _MISSING:
        return bool(is_error)
    content = getattr(response, "content", "") or ""
    return content.startswith(_LEGACY_ERROR_PREFIXES)


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
