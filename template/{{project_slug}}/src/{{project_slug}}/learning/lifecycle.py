"""
Conditional lifecycle writes for corrections.

A status transition (approve/reject/retire) must only land while the row
still holds the expected prior status; otherwise two racing operators
can both "win" and silently overwrite each other (the classic
read-check-write TOCTOU). transition() closes that gap with the store's
update_if primitive -- one conditional UPDATE -- and raises ValueError
on a lost race so the API can answer 409 Conflict.

Bring-your-own-backend compatibility: custom stores that predate
update_if fall back to the old unconditional write with a one-time
warning (the race window returns with that fallback).

Keep this file under 100 lines.
"""

import logging

logger = logging.getLogger(__name__)

_update_if_missing_warned = False


def _warn_update_if_missing_once(store_name: str) -> None:
    global _update_if_missing_warned
    if _update_if_missing_warned:
        return
    _update_if_missing_warned = True
    logger.warning(
        f"[Corrections] Store {store_name!r} has no update_if(); falling "
        "back to unconditional status writes. Two racing lifecycle calls "
        "can both succeed (last write wins). Implement update_if(table, "
        "row_id, changes, expected) -> bool to close the race."
    )


def transition(store, correction_id: str, changes: dict, expected_status: str) -> None:
    """Apply a corrections lifecycle write only while the row still holds
    expected_status (one conditional UPDATE), closing the gap between the
    caller's status pre-check and its write. On a lost race, raises
    ValueError -- the API surfaces that as 409 Conflict. Custom stores
    without update_if get the old unconditional write plus a one-time
    warning (bring-your-own-backend compatibility)."""
    update_if = getattr(store, "update_if", None)
    if not callable(update_if):
        _warn_update_if_missing_once(type(store).__name__)
        store.update("corrections", correction_id, changes)
        return
    if not update_if(
        "corrections", correction_id, changes, {"status": expected_status}
    ):
        rows = store.query("corrections", {"id": correction_id}, limit=1)
        now = rows[0].get("status", "unknown") if rows else "deleted"
        raise ValueError(
            f"Correction {correction_id} is no longer '{expected_status}' "
            f"(now '{now}'): a concurrent lifecycle change won the race"
        )
