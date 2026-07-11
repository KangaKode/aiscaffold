"""
Opt-in detection wiring for the API gateway (every toggle default OFF).

Hooks the gateway initializes at startup, each gated by its own env
toggle and each detect-only (integrity flags and logs; nothing is ever
blocked or altered):

  BASELINE_TRACKING_ENABLED   -- AgentBaselineTracker on app.state,
                                 threaded into round-table/chat dispatch
                                 (learning/activity.py).
  COLLUSION_DETECTION_ENABLED -- ONE per-process CollusionDetector on
                                 app.state, fed each deliberation's votes
                                 (learning/collusion.py; the instance
                                 accumulates cross-round vote history).
  STARTUP_CANARY_ENABLED      -- one-shot self-check that the canary
                                 injection/detection machinery
                                 round-trips (security/injection_defense).

Everything here is fire-and-forget: failures are logged (and the canary
failure recorded as an integrity flag) but never abort gateway startup
and never fail a request.

Keep this file under 200 lines.
"""

import logging
import os

from ..learning.activity import create_baseline_tracker
from ..learning.collusion import create_collusion_detector
from ..learning.flags import insert_flag_once
from ..security import check_canary, inject_canary
from ..security.prompt_guard import sanitize_for_prompt, wrap_user_content

logger = logging.getLogger(__name__)

CANARY_FLAG_TYPE = "startup_canary_failed"

_TOGGLES = (
    "BASELINE_TRACKING_ENABLED",
    "COLLUSION_DETECTION_ENABLED",
    "STARTUP_CANARY_ENABLED",
)


def _enabled(var: str) -> bool:
    return os.environ.get(var, "").strip().lower() in ("true", "1", "yes")


def init_detection_hooks(application) -> None:
    """Initialize the opt-in detection hooks on the FastAPI app state.

    With every toggle off (the default) this sets the two app.state
    attributes to None and returns -- downstream code treats None as
    "wiring off" and behaves exactly as before.
    """
    store = getattr(application.state, "learning_store", None)
    if store is None and any(_enabled(v) for v in _TOGGLES):
        # Activity tracking may be disabled while a detection hook is
        # enabled; the hooks only need a store, not the middleware.
        try:
            from ..learning.store import get_learning_store

            store = get_learning_store()
        except Exception as e:
            logger.warning(
                f"[Gateway] Detection hooks: learning store unavailable "
                f"({type(e).__name__}) -- enabled hooks degrade to no-ops"
            )
            store = None

    application.state.baseline_tracker = create_baseline_tracker(store)
    if application.state.baseline_tracker is not None:
        logger.info("[Gateway] Agent baseline tracking enabled (detect-only)")

    application.state.collusion_detector = create_collusion_detector(
        store,
        checkin_manager=getattr(application.state, "checkin_manager", None),
    )
    if application.state.collusion_detector is not None:
        logger.info("[Gateway] Collusion detection enabled (detect-only)")

    run_startup_canary_check(store)


def run_startup_canary_check(store=None) -> bool | None:
    """One-shot startup self-check of the canary machinery (opt-in).

    Verifies, with the shipped functions only, that:
      1. ``wrap_user_content(canary=True)`` embeds a detectable canary in
         the outbound prompt content, and the client's outbound
         sanitization (``sanitize_for_prompt``) preserves it -- a canary
         stripped before the provider call could never detect anything;
      2. ``check_canary`` detects the token in a simulated leaked
         response;
      3. ``check_canary`` does NOT fire on a clean response;
      4. consecutive injections mint distinct tokens (a predictable
         canary is trivially evaded).

    This is a startup self-test of the library round-trip, NOT runtime
    response scanning: no live LLM response is ever checked here. On
    failure the outcome is logged and recorded as a
    ``startup_canary_failed`` integrity flag (severity error) when a
    learning store is available. Never raises, never blocks startup.
    Returns True on success, False on failure, None when the toggle is
    off.
    """
    if not _enabled("STARTUP_CANARY_ENABLED"):
        return None
    try:
        failures: list[str] = []
        sample = "Summarize the deployment status for the operations report."

        wrapped, canary = wrap_user_content(
            sample, label="STARTUP_CANARY", canary=True
        )
        if canary not in wrapped:
            failures.append("canary_missing_from_wrapped_prompt")
        elif canary not in sanitize_for_prompt(wrapped):
            failures.append("canary_lost_in_outbound_sanitization")

        if not check_canary(f"simulated response leaking {canary} verbatim", canary):
            failures.append("leak_not_detected")
        if check_canary("a clean simulated response", canary):
            failures.append("false_positive_on_clean_response")

        _, second = inject_canary(sample, "STARTUP_CANARY")
        if second == canary:
            failures.append("canary_tokens_not_unique")

        if not failures:
            logger.info("[Gateway] Startup canary self-check passed")
            return True
        logger.error(
            f"[Gateway] Startup canary self-check FAILED: {failures} -- "
            f"canary-based breach detection cannot be trusted"
        )
        _record_canary_failure(store, {"failures": failures})
        return False
    except Exception as e:
        # Even a crashed self-check must never block startup.
        logger.error(
            f"[Gateway] Startup canary self-check errored (ignored): "
            f"{type(e).__name__}: {e}"
        )
        _record_canary_failure(store, {"error": f"{type(e).__name__}: {e}"})
        return False


def _record_canary_failure(store, detail: dict) -> None:
    """Best-effort integrity flag for a failed canary self-check."""
    if store is None:
        return
    try:
        insert_flag_once(
            store,
            CANARY_FLAG_TYPE,
            subject_id="gateway",
            tenant_id="default",
            detail=detail,
            severity="error",
        )
    except Exception as e:
        logger.warning(f"[Gateway] Canary failure flag not persisted: {e}")
