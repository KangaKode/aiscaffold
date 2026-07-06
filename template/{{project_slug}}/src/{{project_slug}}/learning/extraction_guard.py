"""
Extraction guard -- knowledge-read volume monitoring (detection-first).

Approved corrections and error schemas are the platform's accumulated
institutional knowledge; an insider who can read them can also bulk-copy
them. This module counts recent reads of knowledge endpoints (GET
/corrections and friends) in activity_events over a rolling window and
maps the counts to a mode:

  normal   -- both counts within thresholds; nothing happens.
  elevated -- per-user count exceeds EXTRACTION_USER_THRESHOLD or the
              tenant aggregate exceeds EXTRACTION_TENANT_THRESHOLD;
              a cooldown-deduped integrity flag is written.
  capped   -- either count exceeds 2x its threshold; a higher-severity
              flag is written.

Detection-only by default: elevated/capped write integrity flags for
human review and NOTHING else changes -- prompts, knowledge context, and
listings are never touched, and listings are never silently truncated.
The single opt-in enforcement point (EXTRACTION_GUARD_ENFORCE=true) is
GET /corrections returning 429 with a Retry-After header while capped.

Degradation: when activity tracking is off (ACTIVITY_TRACKING_ENABLED=
false) or no store is available, there is nothing to count -- the guard
returns "normal" and warns once that extraction defense is inert.

Identity caveat: user_id is a SHA-256 prefix of the bearer token. The
vanilla scaffold ships a SINGLE API_KEY, so every caller shares one
user_id ("anon" in keyless dev mode). In that deployment the per-user
threshold is effectively a global threshold, and one abuser can trip the
cap for everyone sharing the key. Multi-key deployments should extend
auth (see PLATFORM_GUIDE.md step 1) for real per-user identity.

Env tunables: EXTRACTION_USER_THRESHOLD (default 60),
EXTRACTION_TENANT_THRESHOLD (default 300), EXTRACTION_WINDOW_MINUTES
(default 60), EXTRACTION_GUARD_ENFORCE (default false).

Leaf module: imports stdlib + learning.flags only; the store is passed in.

Keep this file under 250 lines.
"""

import logging
import os
from datetime import datetime, timedelta

from .flags import insert_flag_once

logger = logging.getLogger(__name__)

FLAG_TYPE_EXTRACTION = "extraction_volume_anomaly"

MODE_NORMAL = "normal"
MODE_ELEVATED = "elevated"
MODE_CAPPED = "capped"

# Knowledge-read endpoints counted by the guard (GET only). Extension
# point: add routes that serve accumulated knowledge in your deployment.
KNOWLEDGE_READ_PREFIXES = (
    "/api/v1/corrections",
    "/api/v1/reflections",
)

# Capped kicks in at this multiple of a threshold.
CAP_MULTIPLIER = 2

# Max rows fetched when computing the rolling window (equality-filter
# store; window filtered in Python -- same tradeoff as learning/activity.py).
WINDOW_FETCH_CAP = 2000

DEFAULT_USER_THRESHOLD = 60
DEFAULT_TENANT_THRESHOLD = 300
DEFAULT_WINDOW_MINUTES = 60

_inert_warned = False


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes")


def enforcement_enabled() -> bool:
    """Opt-in 429 enforcement on GET /corrections (default off)."""
    return _env_flag("EXTRACTION_GUARD_ENFORCE", default=False)


def retry_after_seconds() -> int:
    """Retry-After value: one rolling window, in seconds."""
    return _env_int("EXTRACTION_WINDOW_MINUTES", DEFAULT_WINDOW_MINUTES) * 60


def warn_inert_once(reason: str) -> None:
    """Warn (once per process) that extraction defense cannot observe
    anything -- called at gateway startup and defensively on evaluate."""
    global _inert_warned
    if _inert_warned:
        return
    _inert_warned = True
    logger.warning(
        f"[ExtractionGuard] Extraction defense is inert: {reason}. "
        "Knowledge-read volume is not being monitored."
    )


def _tracking_enabled() -> bool:
    return os.environ.get("ACTIVITY_TRACKING_ENABLED", "true").strip().lower() not in (
        "false", "0", "no",
    )


def _mode_from_counts(
    user_count: int, tenant_count: int, user_threshold: int, tenant_threshold: int
) -> str:
    if (
        user_count > user_threshold * CAP_MULTIPLIER
        or tenant_count > tenant_threshold * CAP_MULTIPLIER
    ):
        return MODE_CAPPED
    if user_count > user_threshold or tenant_count > tenant_threshold:
        return MODE_ELEVATED
    return MODE_NORMAL


def evaluate_extraction_mode(
    store, user_id: str = "", tenant_id: str = "default"
) -> dict:
    """
    Compute the current extraction mode for a caller.

    Counts GETs of knowledge endpoints in activity_events over the last
    EXTRACTION_WINDOW_MINUTES, per user AND tenant-wide. An empty/missing
    user_id falls back to tenant-scope counting only. Elevated/capped
    modes persist a cooldown-deduped integrity flag; the caller decides
    whether anything else happens (detection-only by default).

    Returns {"mode": ..., "user_count": ..., "tenant_count": ...}.
    Never raises: any failure degrades to normal.
    """
    result = {"mode": MODE_NORMAL, "user_count": 0, "tenant_count": 0}
    if store is None or not _tracking_enabled():
        warn_inert_once(
            "activity tracking is disabled or no learning store is available"
        )
        return result

    user_threshold = _env_int("EXTRACTION_USER_THRESHOLD", DEFAULT_USER_THRESHOLD)
    tenant_threshold = _env_int(
        "EXTRACTION_TENANT_THRESHOLD", DEFAULT_TENANT_THRESHOLD
    )
    window_minutes = _env_int("EXTRACTION_WINDOW_MINUTES", DEFAULT_WINDOW_MINUTES)

    try:
        rows = store.query(
            "activity_events",
            {"tenant_id": tenant_id},
            order_by="created_at DESC",
            limit=WINDOW_FETCH_CAP,
        )
    except Exception as exc:
        logger.warning(f"[ExtractionGuard] activity query failed (ignored): {exc}")
        return result

    cutoff = (datetime.now() - timedelta(minutes=window_minutes)).isoformat()
    knowledge_reads = [
        r
        for r in rows
        if r.get("created_at", "") >= cutoff
        and r.get("method", "").upper() == "GET"
        and any(r.get("route", "").startswith(p) for p in KNOWLEDGE_READ_PREFIXES)
    ]
    tenant_count = len(knowledge_reads)
    user_count = (
        sum(1 for r in knowledge_reads if r.get("user_id", "") == user_id)
        if user_id
        else 0
    )

    mode = _mode_from_counts(user_count, tenant_count, user_threshold, tenant_threshold)
    result.update({"mode": mode, "user_count": user_count, "tenant_count": tenant_count})

    if mode != MODE_NORMAL:
        detail = {
            "kind": "extraction_volume",
            "mode": mode,
            "user_count": user_count,
            "tenant_count": tenant_count,
            "user_threshold": user_threshold,
            "tenant_threshold": tenant_threshold,
            "window_minutes": window_minutes,
        }
        logger.warning(f"[ExtractionGuard] Knowledge-read volume {mode}: {detail}")
        insert_flag_once(
            store,
            FLAG_TYPE_EXTRACTION,
            subject_id=user_id or tenant_id,
            tenant_id=tenant_id,
            detail=detail,
            severity="warning" if mode == MODE_ELEVATED else "error",
        )
    return result
