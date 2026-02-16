"""
JSON extraction from LLM responses -- handles real-world LLM output quirks.

LLMs frequently wrap JSON in markdown code fences, add preamble text, or
return slightly malformed JSON. This module extracts valid JSON from messy
LLM output instead of failing on bare json.loads().

Usage:
    from ..llm.json_parser import extract_json

    data = extract_json(response.content)
    if data is None:
        # Handle parse failure
"""

import json
import logging
import re

logger = logging.getLogger(__name__)


def extract_json(text: str) -> dict | list | None:
    """
    Extract JSON from LLM output, handling common formatting issues.

    Tries in order:
      1. Direct json.loads (clean output)
      2. Strip markdown code fences (```json ... ```)
      3. Find first { or [ and parse from there
      4. Return None if all fail

    Returns parsed JSON (dict or list) or None.
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    # Try 1: Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try 2: Strip markdown code fences
    fence_pattern = r"```(?:json)?\s*\n?(.*?)\n?\s*```"
    fence_match = re.search(fence_pattern, text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try 3: Find first { or [ and extract to matching close
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start_idx = text.find(start_char)
        if start_idx == -1:
            continue
        end_idx = text.rfind(end_char)
        if end_idx <= start_idx:
            continue
        candidate = text[start_idx : end_idx + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    logger.warning(
        f"[JSONParser] Failed to extract JSON from LLM output ({len(text)} chars)"
    )
    return None


def extract_json_or_raise(text: str, context: str = "LLM response") -> dict | list:
    """Extract JSON or raise ValueError with context."""
    result = extract_json(text)
    if result is None:
        raise ValueError(
            f"Could not parse JSON from {context}. "
            f"Raw output ({len(text)} chars): {text[:200]}..."
        )
    return result
