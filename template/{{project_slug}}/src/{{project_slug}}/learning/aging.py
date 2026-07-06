"""
Knowledge aging -- staleness derivation for approved corrections.

Approved corrections stay in prompts indefinitely, but knowledge rots:
a rate limit changes, an API is renamed, and last year's correction is
this year's misinformation. This module derives a "stale" signal so
humans know what to re-check.

Freshness of a correction = last_validated_at when set (someone
explicitly re-confirmed it via POST /corrections/{id}/revalidate),
otherwise updated_at (the last lifecycle change). The fallback means
legacy rows created before the aging columns existed do not all flag
stale on day one -- their approval date keeps counting.

A correction is stale when its freshness timestamp is older than
CORRECTION_STALE_DAYS (env, default 90). Staleness never changes
behavior by itself: stale corrections keep grounding prompts until a
human retires or revalidates them. Surfacing happens via
GET /corrections?stale=true and the governance report.

Leaf module: stdlib only.

Keep this file under 100 lines.
"""

import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DEFAULT_STALE_DAYS = 90


def stale_days() -> int:
    """Staleness threshold in days (CORRECTION_STALE_DAYS env, default 90)."""
    raw = os.environ.get("CORRECTION_STALE_DAYS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_STALE_DAYS


def freshness_timestamp(
    last_validated_at: str, updated_at: str = "", created_at: str = ""
) -> str:
    """The timestamp staleness is measured from, with legacy fallbacks."""
    return last_validated_at or updated_at or created_at


def is_stale(
    last_validated_at: str,
    updated_at: str = "",
    created_at: str = "",
    now: datetime | None = None,
    threshold_days: int | None = None,
) -> bool:
    """
    True when the correction's freshness timestamp is older than the
    threshold. Missing or unparseable timestamps yield False (never
    flag rows we cannot date -- staleness is a review aid, not a gate).
    """
    stamp = freshness_timestamp(last_validated_at, updated_at, created_at)
    if not stamp:
        return False
    try:
        fresh_at = datetime.fromisoformat(stamp)
    except ValueError:
        logger.debug(f"[Aging] Unparseable freshness timestamp: {stamp!r}")
        return False
    reference = now or datetime.now()
    return fresh_at < reference - timedelta(
        days=threshold_days if threshold_days is not None else stale_days()
    )


def row_is_stale(
    row: dict, now: datetime | None = None, threshold_days: int | None = None
) -> bool:
    """is_stale() over a corrections-table row dict."""
    return is_stale(
        row.get("last_validated_at") or "",
        row.get("updated_at") or "",
        row.get("created_at") or "",
        now=now,
        threshold_days=threshold_days,
    )
