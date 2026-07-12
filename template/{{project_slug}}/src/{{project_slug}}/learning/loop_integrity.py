"""
Activation + wiring for the two dormant loop-integrity detectors.

Two detectors that shipped as tested libraries with no runtime call site
are given ONE shared, opt-in, detect-only wiring here:

  * correction drift (learning-loop detector) -- ``analyze_correction_drift``
    in ``collusion.py``: a slow-poisoning heuristic over approved corrections,
    run on the corrections-APPROVE path.
  * multi-turn poisoning (conversation-level injection helper) --
    ``detect_multi_turn_poisoning`` in ``security/injection_defense.py``:
    scans a bounded window of recent user messages for setup->action
    injection spread across turns.

Both are DETECT-ONLY: they only log and write ``integrity_flags`` rows. No
message is blocked, no approval delayed-fail, no prompt altered. The
IMPLEMENTATIONS live in their home modules; this module is activation +
flag/store plumbing only.

One shared env flag (``LOOP_INTEGRITY_DETECTION_ENABLED``, default off)
governs both -- a deliberate divergence from one-flag-per-feature to respect
the env-flag budget, since both are the same "loop-integrity" opt-in.

Why the poisoning scan is NOT sampled: it needs ordered setup->action
detection, and a setup phrase can age out of the window between sampled
passes; a per-turn capped-regex scan is cheap enough to run every turn.

Keep this file under 150 lines.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from ..security.injection_defense import detect_multi_turn_poisoning
from .collusion import FLAG_TYPE_DRIFT, analyze_correction_drift
from .flags import max_unresolved_severity, record_flag_hit

logger = logging.getLogger(__name__)

LOOP_INTEGRITY_ENV = "LOOP_INTEGRITY_DETECTION_ENABLED"
LOOP_INTEGRITY_WINDOW_ENV = "LOOP_INTEGRITY_SCAN_WINDOW_MESSAGES"
_DEFAULT_WINDOW = 20
_MIN_WINDOW = 2

FLAG_TYPE_POISONING = "multi_turn_poisoning"
_DRIFT_SUBJECT = "corrections"


def loop_integrity_enabled() -> bool:
    """True when the shared opt-in flag is set (default off)."""
    return os.environ.get(LOOP_INTEGRITY_ENV, "").strip().lower() in ("true", "1", "yes")


def scan_window_size() -> int:
    """Message window for the poisoning scan; bad/low values clamp to default."""
    try:
        val = int(os.environ.get(LOOP_INTEGRITY_WINDOW_ENV, ""))
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW
    return val if val >= _MIN_WINDOW else _DEFAULT_WINDOW


def create_drift_check_hook(store, tenant_id: str = "default") -> Callable | None:
    """Correction-drift check for CorrectionsManager.on_approve / the gateway.

    Returns None when the flag is off (so no callback is wired at all). The
    returned callable is ``_hook(correction=None)`` -- the gateway calls it
    with no argument, and CorrectionsManager calls it with the approved
    ``Correction`` (whose tenant scopes the scan); when no correction is
    passed the bound ``tenant_id`` is used.

    Cooldown: before the ~120-row analysis (which raw-inserts a warning-level
    drift flag), skip when an unresolved ``correction_drift`` flag already
    exists for the tenant -- otherwise the un-rate-limited approve path would
    flood duplicate flags. A broken store fails toward "already flagged"
    (``max_unresolved_severity`` returns "error"), which also skips -- no
    flood, no request impact. All exceptions are swallowed to a WARNING.
    """
    if not loop_integrity_enabled():
        return None

    def _hook(correction=None) -> None:
        tid = getattr(correction, "tenant_id", None) or tenant_id
        try:
            if max_unresolved_severity(store, FLAG_TYPE_DRIFT, _DRIFT_SUBJECT, tid) is not None:
                return
            analyze_correction_drift(store, tenant_id=tid)
        except Exception as exc:  # noqa: BLE001 -- detect-only, never fatal
            logger.warning(f"[LoopIntegrity] Correction-drift check failed: {exc}")

    return _hook


def scan_conversation_window(
    history: list,
    store=None,
    tenant_id: str = "default",
    subject_id: str = "",
) -> list[str]:
    """Detect-only multi-turn poisoning scan over the last-N user messages.

    Flag check happens FIRST, before any message access, so a flag-off call
    is zero-message-access (not merely zero-work at the caller). On findings,
    records one ``multi_turn_poisoning`` integrity flag (insert-or-update, so
    a sustained campaign escalates rather than flooding) keyed by
    ``subject_id``; with no store the finding is log-only. Never raises.

    Window bound honesty: only the last ``scan_window_size()`` messages are
    scanned, so setup and action must BOTH fall inside that window -- an
    attacker spacing them wider evades detection (documented blind spot).
    """
    if not loop_integrity_enabled():
        return []
    window = scan_window_size()
    recent = history[-window:]
    texts = [
        m.get("content", "")
        for m in recent
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    findings = detect_multi_turn_poisoning(texts)
    if not findings:
        return findings
    detail = {"findings": findings[:10], "window_messages": len(recent)}
    if store is None:
        logger.warning(
            f"[LoopIntegrity] Multi-turn poisoning (log-only, no store): {detail}"
        )
        return findings
    try:
        record_flag_hit(
            store, FLAG_TYPE_POISONING, subject_id or "chat", tenant_id, detail
        )
    except Exception as exc:  # noqa: BLE001 -- detect-only, never fatal
        logger.warning(f"[LoopIntegrity] Poisoning flag write failed: {exc}")
    return findings
