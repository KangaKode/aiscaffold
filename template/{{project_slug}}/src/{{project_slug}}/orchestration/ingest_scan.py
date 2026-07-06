"""
Detect-only Layer 1 injection scan for user-message ingestion surfaces.

User messages are legitimate content that may discuss injection techniques
(a security review quoting a jailbreak phrase is not an attack), so this
scan NEVER blocks the request and NEVER mutates the message. It exists to
give operators signal: findings are logged and, when a learning store is
in scope, persisted as an integrity flag (deduplicated per surface by the
standard unresolved-flag cooldown in learning/flags.py).

Surfaces wired at ship time:
  - chat            api/routes/chat.py (message + stream routes)
  - resolve         orchestration/single_shot.py (single-shot tier)
  - premise_gate    orchestration/premise.py (log-only; no store in scope)
  - round_table     api/routes/round_table.py (task submission)

Layer 1 only (advanced=False) by design: the Layer 2 decoding pass is for
machine-generated boundaries (tool output, agent responses, knowledge
writes) where encoded payloads are anomalous; user prose that discusses
base64 or quotes encoded examples would false-positive too often.

Keep this file under 100 lines.
"""

import logging

from ..security.prompt_guard import detect_injection_attempt

logger = logging.getLogger(__name__)

FLAG_TYPE = "user_message_injection"
_MAX_PATTERNS_IN_DETAIL = 10  # cap the pattern list persisted per flag


def scan_user_message(
    text: str,
    surface: str,
    store=None,
    tenant_id: str = "default",
) -> list[str]:
    """Run the Layer 1 pattern scan on a user message, detect-only.

    Returns the findings (empty = clean) so callers can add their own
    telemetry, but callers MUST NOT use them to block or rewrite the
    message -- that would break legitimate discussion of injection
    techniques. Findings are logged; when ``store`` is provided, one
    integrity flag per (surface, tenant) is persisted until a human
    resolves it (see learning/flags.py cooldown semantics). The flag
    stores which patterns matched, never the message content.

    Best-effort by construction: a failure anywhere in the scan or the
    flag write is logged and swallowed -- ingestion never fails because
    detection did.
    """
    try:
        findings = detect_injection_attempt(text)
    except Exception:
        logger.warning(
            "[IngestScan] Layer 1 scan failed on surface %s (non-fatal)",
            surface,
            exc_info=True,
        )
        return []

    if not findings:
        return []

    logger.warning(
        "[IngestScan] Injection patterns in user message on surface %s: "
        "%d finding(s) (detect-only, request proceeds unmodified)",
        surface,
        len(findings),
    )

    if store is not None:
        try:
            from ..learning.flags import insert_flag_once

            insert_flag_once(
                store,
                flag_type=FLAG_TYPE,
                subject_id=surface,
                tenant_id=tenant_id,
                detail={
                    "surface": surface,
                    "patterns": findings[:_MAX_PATTERNS_IN_DETAIL],
                    "message_chars": len(text),
                },
                severity="warning",
            )
        except Exception:
            logger.warning(
                "[IngestScan] Failed to persist integrity flag for surface %s "
                "(non-fatal)",
                surface,
                exc_info=True,
            )

    return findings
