"""
Four-eyes enforceability policy for the corrections lifecycle.

The four-eyes rule (approver must differ from proposer) assumes callers
have distinct identities. Under the scaffold's single-API-key default,
every caller shares one user_id, so created_by == approved_by ALWAYS:
strict enforcement makes approval impossible, silent enforcement makes
the control theater. This module makes the posture explicit:

  CORRECTIONS_FOUR_EYES=strict  Self-approval is rejected with a clear
                                error. Correct once real multi-user
                                identities exist (see PLATFORM_GUIDE.md).
  CORRECTIONS_FOUR_EYES=warn    (default) Self-approval is ALLOWED, but
                                each occurrence logs loudly and a
                                "four_eyes_unenforceable" integrity flag
                                is recorded once per (tenant, approver)
                                until a human resolves it -- the control
                                degrades visibly, never silently.

Any other value falls back to "warn" with a warning. The env var is read
per approval, so tests and operators can flip modes without restart.
Consumed by CorrectionsManager when constructed with
require_four_eyes=None (the API gateway wiring); explicit True/False
keep their historical strict/off semantics for library callers.

Leaf module: stdlib + learning.flags only.

Keep this file under 160 lines.
"""

import logging
import os

from .flags import insert_flag_once

logger = logging.getLogger(__name__)

FOUR_EYES_STRICT = "strict"
FOUR_EYES_WARN = "warn"

FLAG_TYPE_FOUR_EYES = "four_eyes_unenforceable"


def four_eyes_mode() -> str:
    """Resolve CORRECTIONS_FOUR_EYES (default "warn"; invalid values warn)."""
    raw = os.environ.get("CORRECTIONS_FOUR_EYES", "").strip().lower()
    if raw in (FOUR_EYES_STRICT, FOUR_EYES_WARN):
        return raw
    if raw:
        logger.warning(
            f"[FourEyes] Invalid CORRECTIONS_FOUR_EYES={raw!r} "
            f"(expected '{FOUR_EYES_STRICT}' or '{FOUR_EYES_WARN}'); "
            f"using '{FOUR_EYES_WARN}'"
        )
    return FOUR_EYES_WARN


def record_self_approval(
    store, correction_id: str, approved_by: str, tenant_id: str = "default"
) -> None:
    """Loudly surface an allowed self-approval (warn mode).

    Logs a warning on every occurrence and persists ONE unresolved
    "four_eyes_unenforceable" integrity flag per (tenant, approver)
    (insert_flag_once cooldown) so operators see that the four-eyes
    control is not enforceable with the current identity setup.
    Best-effort: never raises, never blocks the approval.
    """
    logger.warning(
        f"[FourEyes] Correction {correction_id} was approved by its own "
        f"proposer '{approved_by}' (CORRECTIONS_FOUR_EYES=warn). Four-eyes "
        "review is NOT enforceable under a single shared identity -- set up "
        "multi-user auth (docs/PLATFORM_GUIDE.md) and CORRECTIONS_FOUR_EYES="
        "strict to enforce it, or leave warn mode to keep single-operator "
        "approvals working with this visible trail."
    )
    if store is None:
        return
    try:
        insert_flag_once(
            store,
            FLAG_TYPE_FOUR_EYES,
            subject_id=approved_by or "unknown",
            tenant_id=tenant_id,
            detail={
                "correction_id": correction_id,
                "reason": (
                    "approver == proposer; four-eyes not enforceable "
                    "under single-key identity"
                ),
                "mode": FOUR_EYES_WARN,
            },
        )
    except Exception as exc:  # insert_flag_once already guards; belt+braces
        logger.warning(f"[FourEyes] flag recording failed (ignored): {exc}")


def check_four_eyes(
    store,
    correction_id: str,
    created_by: str,
    approved_by: str,
    tenant_id: str = "default",
    require: bool | None = None,
) -> None:
    """Apply the four-eyes posture to an approval attempt.

    No-op unless approver == proposer (with a non-empty proposer).
    require=True pins strict, require=False disables the check entirely
    (historical library semantics), and require=None defers to
    CORRECTIONS_FOUR_EYES. Strict raises ValueError; warn (default)
    allows the approval but logs loudly and records an integrity flag
    via record_self_approval.
    """
    if not created_by or approved_by != created_by:
        return
    if require is False:
        return
    mode = FOUR_EYES_STRICT if require is True else four_eyes_mode()
    if mode == FOUR_EYES_STRICT:
        raise ValueError(
            f"Four-eyes rule: correction {correction_id} was proposed "
            f"by '{created_by}' and cannot be approved by the same user "
            "(CORRECTIONS_FOUR_EYES=strict). Have a second reviewer "
            "approve it, or run warn mode for single-operator deployments."
        )
    record_self_approval(store, correction_id, approved_by, tenant_id=tenant_id)
