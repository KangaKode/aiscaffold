"""
ContentPolicy -- heuristic classifier for knowledge writes.

Text that gets persisted into the learning system (corrections, notes,
preferences) can steer future agent behavior, which makes it a target
for policy manipulation: "always classify X as safe", "don't log this",
"increase the trust score". This module classifies such text BEFORE it
is stored.

Outcomes:
  - "approved": no policy category matched.
  - "flagged": one soft (medium-confidence) hit -- store it, but surface
    for human review.
  - "rejected": a high-confidence hit or multiple category hits --
    callers should refuse the write.

Categories (all regex/keyword heuristics, documented inline):
  - behavioral_directive: blanket always/never rules for classification
    or approval ("always classify X as safe", "never flag", ...)
  - severity_manipulation: instructions to downgrade or minimize risk
    ("downgrade severity", "treat as low risk", ...)
  - evasion_instruction: instructions to avoid logging/auditing or
    detection ("don't log", "hide from audit", ...)
  - trust_manipulation: instructions to inflate trust or skip
    verification ("increase trust score", "mark as verified without")

Relationship to OverrideDetector: OverrideDetector screens for prompt
injection and attacks on the oversight agents (injection patterns,
safety-agent targeting, evidence inflation); ContentPolicy screens for
POLICY-shaped manipulation -- text that reads as a legitimate knowledge
write but tries to install a standing rule. Run both on corrections;
they complement rather than replace each other.

Heuristics have false positives and negatives by nature: "flagged" only
adds a human review step, and even "rejected" text is recorded as an
integrity flag so a reviewer can see what was refused.

Keep this file under 250 lines.
"""

import json
import logging
import re
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

OUTCOME_APPROVED = "approved"
OUTCOME_FLAGGED = "flagged"
OUTCOME_REJECTED = "rejected"

FLAG_TYPE_CONTENT_POLICY = "content_policy"

MAX_CLASSIFY_CHARS = 15_000

# High-confidence patterns: a single hit rejects. These are phrasings
# with essentially no legitimate use in a knowledge write.
_HARD_PATTERNS: dict[str, list[str]] = {
    "behavioral_directive": [
        r"\b(?:always|never)\s+(?:classify|flag|mark|treat|approve|reject|block|allow)\b",
        r"\b(?:automatically|auto)[\s-]?approve\b",
    ],
    "severity_manipulation": [
        r"\bdowngrade\s+(?:the\s+)?(?:severity|priority|risk)\b",
        r"\bset\s+(?:all|every|any)\s+(?:severity|priority|risk)\s+to\s+(?:low|none|info)\b",
    ],
    "evasion_instruction": [
        r"\b(?:don'?t|do\s+not|never)\s+(?:log|record|audit|report)\b",
        r"\bhide\s+(?:this\s+)?from\s+(?:the\s+)?(?:audit|log|review)",
        r"\bavoid\s+(?:detection|triggering|being\s+(?:logged|flagged|detected))\b",
        r"\bwithout\s+(?:logging|leaving\s+a\s+(?:trace|record))\b",
    ],
    "trust_manipulation": [
        r"\b(?:increase|boost|raise|max)\s+(?:the\s+)?trust\s+scores?\b",
        r"\bmark\s+(?:as\s+)?(?:verified|trusted|approved)\s+without\b",
        r"\bskip\s+(?:the\s+)?(?:verification|approval|review)\s+(?:step|process)\b",
    ],
}

# Soft patterns: policy-adjacent language that is sometimes legitimate.
# One soft hit -> flagged; soft hits in 2+ categories -> rejected.
_SOFT_PATTERNS: dict[str, list[str]] = {
    "behavioral_directive": [
        r"\bshould\s+(?:always|never|generally)\s+be\s+(?:treated|classified|considered)\b",
        r"\bfrom\s+now\s+on\b",
    ],
    "severity_manipulation": [
        r"\btreat\s+(?:\S+\s+){0,4}as\s+(?:low[\s-]risk|benign|harmless|noise)\b",
        r"\b(?:is|are)\s+(?:usually|generally|typically)\s+(?:noise|false\s+positives?)\b",
    ],
    "evasion_instruction": [
        r"\bno\s+need\s+to\s+(?:log|record|report|escalate)\b",
        r"\bquietly\b",
    ],
    "trust_manipulation": [
        r"\b(?:is|are)\s+(?:always|generally)\s+(?:reliable|trustworthy|accurate)\b",
        r"\bcan\s+be\s+trusted\s+without\b",
    ],
}


def _compile(patterns: dict[str, list[str]]) -> dict[str, list[re.Pattern]]:
    return {
        category: [re.compile(p, re.IGNORECASE) for p in pats]
        for category, pats in patterns.items()
    }


_HARD = _compile(_HARD_PATTERNS)
_SOFT = _compile(_SOFT_PATTERNS)


class ContentPolicy:
    """
    Classifies knowledge-write text against manipulation heuristics.

    Usage:
        policy = ContentPolicy(store=get_learning_store())
        outcome, reasons = policy.screen_knowledge_write(text, tenant_id)
        if outcome == "rejected":
            ...refuse the write...

    The store is optional: classify() is pure analysis;
    screen_knowledge_write() skips persistence (with a warning) when no
    store is configured.
    """

    def __init__(self, store=None):
        self._store = store

    def classify(self, text: str) -> tuple[str, list[str]]:
        """
        Classify text. Returns (outcome, reasons) where outcome is one
        of "approved" / "flagged" / "rejected" and reasons are
        human-readable strings naming the matched category and pattern.

        Rejected requires a high-confidence hit OR soft hits across
        multiple categories; a single soft hit only flags.
        """
        if not text or not text.strip():
            return OUTCOME_APPROVED, []

        sample = text[:MAX_CLASSIFY_CHARS]
        hard_reasons: list[str] = []
        soft_categories: set[str] = set()
        soft_reasons: list[str] = []

        for category, patterns in _HARD.items():
            for pattern in patterns:
                if pattern.search(sample):
                    hard_reasons.append(f"{category}: {pattern.pattern}")
                    break

        for category, patterns in _SOFT.items():
            for pattern in patterns:
                if pattern.search(sample):
                    soft_categories.add(category)
                    soft_reasons.append(f"{category} (soft): {pattern.pattern}")
                    break

        if hard_reasons or len(soft_categories) >= 2:
            return OUTCOME_REJECTED, hard_reasons + soft_reasons
        if soft_reasons:
            return OUTCOME_FLAGGED, soft_reasons
        return OUTCOME_APPROVED, []

    def screen_knowledge_write(
        self, text: str, tenant_id: str = "default"
    ) -> tuple[str, list[str]]:
        """
        Classify text and persist an integrity flag when the outcome is
        not "approved" (flag_type="content_policy", severity "warning"
        for flagged / "critical" for rejected). Returns (outcome,
        reasons) like classify(). Flag persistence is best-effort and
        never raises.
        """
        outcome, reasons = self.classify(text)
        if outcome == OUTCOME_APPROVED:
            return outcome, reasons

        logger.warning(
            f"[ContentPolicy] Knowledge write {outcome} with "
            f"{len(reasons)} reason(s): {reasons}"
        )
        if self._store is None:
            logger.warning("[ContentPolicy] No store configured; flag not persisted")
            return outcome, reasons

        try:
            self._store.insert(
                "integrity_flags",
                {
                    "id": str(uuid.uuid4())[:12],
                    "flag_type": FLAG_TYPE_CONTENT_POLICY,
                    "subject_id": "",
                    "tenant_id": tenant_id,
                    "severity": "critical" if outcome == OUTCOME_REJECTED else "warning",
                    "detail_json": json.dumps(
                        {"outcome": outcome, "reasons": reasons}, default=str
                    ),
                    "created_at": datetime.now().isoformat(),
                    "resolved": 0,
                },
            )
        except Exception as exc:
            logger.error(f"[ContentPolicy] Failed to persist flag: {exc}")

        return outcome, reasons
