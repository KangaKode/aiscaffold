"""
OverrideDetector -- screens proposed corrections for manipulation attempts.

Corrections are the main channel through which humans (and agents) reshape
agent behavior, which also makes them the most attractive channel for
attacking it. This module screens each correction for three classes of
abuse BEFORE it reaches a human reviewer:

  1. Prompt injection -- the corrected text or reason contains known
     injection patterns (delegates to security.detect_injection_attempt
     with advanced defenses enabled).
  2. Safety-agent targeting -- instructions that try to neutralize the
     oversight agents ("ignore the skeptic", "bypass fact-check", ...).
  3. Evidence-level inflation -- a correction claiming a high evidence
     level (VERIFIED / CORROBORATED) without any recognizable source
     reference to back it up.

IMPORTANT: screening NEVER auto-rejects. Findings are persisted as
integrity flags so a human decides through the normal check-in /
corrections review flow. A false positive here costs a reviewer a few
seconds; a silent auto-reject could suppress a legitimate correction.

Keep this file under 250 lines.
"""

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from ..security import detect_injection_attempt

logger = logging.getLogger(__name__)

# Instructions aimed at disabling or softening the oversight agents.
# Matched case-insensitively against corrected_claim + reason.
SAFETY_TARGETING_PATTERNS = [
    r"ignore\s+(the\s+)?skeptic",
    r"(skip|bypass|disable|silence|mute)\s+(the\s+)?sentinel",
    r"bypass\s+(the\s+)?fact.?check(er|ing)?",
    r"(skip|disable|ignore)\s+(the\s+)?fact.?check(er|ing)?",
    r"disable\s+(the\s+)?(enforcement|oversight|verification|validation)",
    r"(skip|bypass)\s+(the\s+)?(enforcement|oversight|verification|validation)",
    r"always\s+approve",
    r"never\s+(challenge|dissent|object|refuse|question)",
    r"(auto|automatically)\s*.?approve\s+(all|every)",
    r"do\s+not\s+(challenge|question|verify|check)",
    r"(suppress|hide)\s+(dissent|warnings|flags)",
]

# Evidence levels that require a source reference to be credible.
HIGH_EVIDENCE_LEVELS = ("VERIFIED", "CORROBORATED")

# Source-marker heuristic (v1, intentionally simple): text is considered
# sourced when it contains ANY of:
#   - a "label: value" style source reference (a colon),
#   - a URL (http(s):// or www.),
#   - a document-id-like token (e.g. "DOC-123", "RFC-9110").
# Documented tradeoff: a colon is a weak signal (any colon passes), but
# false negatives here only add a human review step -- never a rejection.
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_DOC_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")

FLAG_TYPE_OVERRIDE = "override_attempt"


def _has_source_marker(text: str) -> bool:
    """True when text contains a colon source ref, URL, or doc-id token."""
    return ":" in text or bool(_URL_RE.search(text)) or bool(_DOC_ID_RE.search(text))


def _field(correction: Any, name: str, default: str = "") -> str:
    """Read a field from a Correction dataclass or a plain dict."""
    if isinstance(correction, dict):
        return str(correction.get(name, default) or default)
    return str(getattr(correction, name, default) or default)


class OverrideDetector:
    """
    Screens corrections for override/manipulation attempts.

    Usage:
        detector = OverrideDetector(store=get_learning_store())
        reasons = detector.screen_and_flag(correction)
        if reasons:
            # correction stays in normal review; flag is already persisted
            ...

    The store is optional: screen() is pure analysis and works without
    persistence; screen_and_flag() logs a warning and skips persistence
    when no store is configured.
    """

    def __init__(self, store=None):
        self._store = store

    def screen(self, correction) -> list[str]:
        """
        Screen a correction (Correction dataclass or dict) for override
        attempts. Returns a list of human-readable flag reasons (empty =
        clean). Pure analysis -- persists nothing, rejects nothing.
        """
        corrected = _field(correction, "corrected_claim")
        reason = _field(correction, "reason")
        evidence_level = _field(correction, "evidence_level").strip().upper()
        combined = f"{corrected}\n{reason}"

        reasons: list[str] = []

        for pattern in detect_injection_attempt(combined, advanced=True):
            reasons.append(f"injection_pattern: {pattern}")

        combined_lower = combined.lower()
        for pattern in SAFETY_TARGETING_PATTERNS:
            if re.search(pattern, combined_lower):
                reasons.append(f"safety_agent_targeting: {pattern}")

        if evidence_level in HIGH_EVIDENCE_LEVELS and not _has_source_marker(combined):
            reasons.append(
                f"evidence_inflation: level '{evidence_level}' claimed without "
                "a source marker (no 'label:' ref, URL, or doc id)"
            )

        return reasons

    def screen_and_flag(self, correction, tenant_id: str = "default") -> list[str]:
        """
        Screen a correction and, when suspicious, persist an integrity
        flag (flag_type="override_attempt", severity="warning").

        NEVER auto-rejects: the correction continues through its normal
        proposed -> approved/rejected lifecycle, and a human decides via
        the check-ins / corrections review flow. The flag just makes the
        suspicion visible.

        Returns the list of flag reasons (empty = clean, nothing persisted).
        """
        reasons = self.screen(correction)
        if not reasons:
            return []

        correction_id = _field(correction, "id")
        logger.warning(
            f"[OverrideDetector] Correction '{correction_id}' flagged with "
            f"{len(reasons)} reason(s): {reasons}"
        )

        if self._store is None:
            logger.warning(
                "[OverrideDetector] No store configured; flag not persisted"
            )
            return reasons

        try:
            self._store.insert(
                "integrity_flags",
                {
                    "id": str(uuid.uuid4())[:12],
                    "flag_type": FLAG_TYPE_OVERRIDE,
                    "subject_id": correction_id,
                    "tenant_id": tenant_id,
                    "severity": "warning",
                    "detail_json": json.dumps({"reasons": reasons}, default=str),
                    "created_at": datetime.now().isoformat(),
                    "resolved": 0,
                },
            )
        except Exception as exc:
            logger.error(f"[OverrideDetector] Failed to persist flag: {exc}")

        return reasons
