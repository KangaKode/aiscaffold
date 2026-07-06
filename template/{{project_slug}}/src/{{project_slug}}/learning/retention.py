"""
Opt-in retention pruning for activity_events.

The activity middleware writes one row per API request, forever. That is
the right default for a scaffold (no data silently disappears), but a
busy deployment accumulates rows without bound. This module deletes
events older than a configured horizon -- OFF unless the operator sets
ACTIVITY_RETENTION_DAYS to a positive integer.

Failure posture (documented in docs/OPERATIONS.md):
  - Default off: unset/0/invalid env means prune_activity_events() is a
    no-op returning 0 -- nothing is ever deleted without an explicit
    opt-in.
  - Bounded work: one call deletes at most `batch_limit` rows (oldest
    first), so a first prune of a huge backlog cannot stall a request;
    successive sampled calls drain the backlog incrementally.
  - Best-effort: storage errors are logged and swallowed; a failed prune
    never fails the request that triggered it, it just leaves rows for
    the next pass.
  - Detection trade-off: rows older than the horizon stop being visible
    to the burst/timing/extraction detectors, whose windows (<= 24h) are
    far shorter than any sane retention horizon.

Leaf module: stdlib only; the store is passed in.

Keep this file under 100 lines.
"""

import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Max rows deleted per call: keeps one sampled middleware pass bounded.
DEFAULT_BATCH_LIMIT = 500


def retention_days() -> int:
    """Configured horizon in days; 0 means retention pruning is off."""
    raw = os.environ.get("ACTIVITY_RETENTION_DAYS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return 0


def prune_activity_events(store, batch_limit: int = DEFAULT_BATCH_LIMIT) -> int:
    """Delete up to batch_limit activity_events older than the horizon.

    Returns the number of rows deleted (0 when disabled, store is None,
    or nothing qualifies). Never raises.
    """
    days = retention_days()
    if days <= 0 or store is None:
        return 0

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    try:
        # Oldest first: equality-only store protocol, so the age filter
        # runs in Python over a bounded batch (same tradeoff as the
        # rolling windows in activity.py).
        rows = store.query(
            "activity_events", {}, order_by="created_at ASC", limit=batch_limit
        )
    except Exception as exc:
        logger.warning(f"[Retention] activity_events query failed (ignored): {exc}")
        return 0

    deleted = 0
    for row in rows:
        if row.get("created_at", "") >= cutoff:
            break  # ascending order: everything after this is newer
        try:
            if store.delete("activity_events", row.get("id", "")):
                deleted += 1
        except Exception as exc:
            logger.warning(f"[Retention] delete failed (ignored): {exc}")
            break

    if deleted:
        logger.info(
            f"[Retention] Pruned {deleted} activity_events older than "
            f"{days} day(s)"
        )
    return deleted
