"""
Procedure write screening -- hard refuse on deny.

Step-shaped knowledge (type=procedure) can steer operators and future
opt-in render paths. This module refuses writes that look like prompt
injection, oversight bypass, or standing behavioral directives -- it does
not soft-warn. Callers map ProcedureScreenError to HTTP 4xx.

Distinct from the Sequence-monitoring "extraction playbook" detector
(attack-pattern language in GOVERNANCE.md).

Keep this file under 200 lines.
"""

from __future__ import annotations

import re

OUTCOME_OK = "ok"
OUTCOME_DENIED = "denied"

# High-confidence hostile / non-procedure patterns: a single hit refuses.
_HARD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "prompt_injection",
        re.compile(
            r"(?i)\b(?:ignore|disregard|forget)\s+(?:all\s+)?"
            r"(?:previous|prior|above)\s+(?:instructions?|rules?|prompts?)\b"
        ),
    ),
    (
        "prompt_injection",
        re.compile(r"(?i)\b(?:system\s*prompt|jailbreak|dan\s*mode)\b"),
    ),
    (
        "oversight_bypass",
        re.compile(
            r"(?i)\b(?:skip|bypass|disable)\s+(?:the\s+)?"
            r"(?:approval|four[\s-]?eyes|human\s+review|screening)\b"
        ),
    ),
    (
        "standing_directive",
        re.compile(
            r"(?i)\b(?:always|never)\s+(?:classify|flag|mark|treat|"
            r"approve|reject|block|allow)\b"
        ),
    ),
    (
        "exfiltration",
        re.compile(
            r"(?i)\b(?:exfiltrat|dump\s+(?:all\s+)?(?:secrets?|keys?|"
            r"credentials?|corrections?))\b"
        ),
    ),
]


class ProcedureScreenError(ValueError):
    """Raised when procedure text must be refused (API maps to 4xx)."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__(
            "Procedure rejected by procedure_screen: " + ", ".join(reasons)
        )


def classify(text: str) -> dict:
    """Return {outcome, reasons} for procedure body text."""
    blob = (text or "")[:15_000]
    reasons: list[str] = []
    for label, pattern in _HARD_PATTERNS:
        if pattern.search(blob) and label not in reasons:
            reasons.append(label)
    if reasons:
        return {"outcome": OUTCOME_DENIED, "reasons": reasons}
    return {"outcome": OUTCOME_OK, "reasons": []}


def screen(*parts: str) -> None:
    """Raise ProcedureScreenError when any part is denied."""
    result = classify("\n".join(p for p in parts if p))
    if result["outcome"] == OUTCOME_DENIED:
        raise ProcedureScreenError(result["reasons"])
