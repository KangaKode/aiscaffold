"""Graded intake for feedback content -- detect/flag, never auto-act.

Screens feedback text with the same Layer 1+2 injection detectors used
elsewhere, persists an integrity flag when findings exist, and returns
reasons so callers can skip trust EMA updates. Does not refuse or rewrite
the feedback row itself.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from ..security import detect_injection_attempt

logger = logging.getLogger(__name__)

FLAG_TYPE_FEEDBACK_INTAKE = "feedback_intake"


def grade_feedback_content(
    content: str,
    *,
    store: Any = None,
    tenant_id: str = "default",
    subject_id: str = "",
) -> list[str]:
    """Return injection flag reasons for feedback content (empty = clean).

    When reasons are non-empty and a store is configured, persists an
    integrity_flags row (flag_type=feedback_intake). Never raises into the
    caller for store failures -- logs and still returns reasons.
    """
    text = (content or "").strip()
    if not text:
        return []

    try:
        findings = detect_injection_attempt(text, advanced=True) or []
    except Exception as exc:  # noqa: BLE001 -- fail closed on trust path
        logger.error("[GradedIntake] detector failed: %s", exc)
        findings = []
        reasons = [f"intake_error: {type(exc).__name__}"]
    else:
        reasons = [f"injection_pattern: {pattern}" for pattern in findings]

    if not reasons:
        return []

    logger.warning(
        "[GradedIntake] Feedback content flagged (%d reason(s)) subject=%s",
        len(reasons),
        subject_id or "(none)",
    )
    if store is None:
        return reasons

    try:
        store.insert(
            "integrity_flags",
            {
                "id": str(uuid.uuid4())[:12],
                "flag_type": FLAG_TYPE_FEEDBACK_INTAKE,
                "subject_id": subject_id or "feedback",
                "tenant_id": tenant_id,
                "severity": "warning",
                "detail_json": json.dumps({"reasons": reasons}, default=str),
                "created_at": datetime.now().isoformat(),
                "resolved": 0,
            },
        )
    except Exception as exc:  # noqa: BLE001 -- detect path must not fail the write
        logger.error("[GradedIntake] Failed to persist feedback flag: %s", exc)

    return reasons
