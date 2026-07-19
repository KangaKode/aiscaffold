"""
Directed proposer->approver pair escalation.

The four-eyes rule requires that a correction's approver differ from its
proposer -- but two colluding accounts satisfy it trivially by always
approving each other's proposals. This module counts DIRECTED
created_by -> approved_by pairs over a rolling window of APPROVED
corrections; a pair whose count exceeds PAIR_APPROVAL_THRESHOLD is
flagged for human review, never auto-blocked.

Directionality matters for escalation: "alice proposes, bob approves"
(A->B) is a different signal from "bob proposes, alice approves" (B->A);
a one-way pipeline and a reciprocal you-scratch-my-back loop warrant
different investigations, so the two directions are counted separately.
Aggregate to unordered pairs only when displaying.

Severity escalates with sustained volume: findings at more than twice
the threshold are persisted as severity="error" instead of "warning".

Known limitations (also stated in GOVERNANCE.md Non-Claims):
  - Rings of 3+ users (A approves B, B approves C, C approves A) never
    concentrate any single directed pair and evade this counter.
  - In single-API-key deployments every caller shares one user_id, so
    created_by always equals approved_by and the four-eyes rule itself
    blocks approval unless it is disabled (require_four_eyes=False) --
    either way there are no distinct pairs to count. Real per-user
    identity (multi-key auth) is a prerequisite for this signal.

Findings persist as integrity_flags (flag_type="approval_pair_dominance",
subject_id="proposer->approver") with a persistence-level cooldown
(see learning/flags.py).

Env tunables: PAIR_APPROVAL_THRESHOLD (default 5),
PAIR_WINDOW_DAYS (default 7).

Leaf module: imports stdlib + learning.flags only; the store is passed in.

Keep this file under 150 lines.
"""

import logging
import os
from datetime import datetime, timedelta

from .flags import insert_flag_once

logger = logging.getLogger(__name__)

FLAG_TYPE_PAIR = "approval_pair_dominance"

# Severity escalates to "error" at this multiple of the threshold.
ESCALATION_MULTIPLIER = 2

# Max approved corrections fetched for the rolling window (equality-filter
# store; window filtered in Python -- same tradeoff as learning/activity.py).
WINDOW_FETCH_CAP = 1000

DEFAULT_THRESHOLD = 5
DEFAULT_WINDOW_DAYS = 7


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else default


def check_pair_dominance(
    store,
    tenant_id: str = "default",
    threshold: int | None = None,
    window_days: int | None = None,
) -> list[dict]:
    """
    Count directed created_by->approved_by pairs over recently APPROVED
    currently-valid corrections and flag pairs above the threshold.

    The window is rolling (last window_days, on the approval timestamp
    updated_at). Self-pairs (created_by == approved_by) and rows missing
    either identity are skipped. Each finding is persisted as a
    cooldown-deduped integrity flag and returned. Fire-and-forget safe:
    query errors are logged and yield [].
    """
    if threshold is None:
        threshold = _env_int("PAIR_APPROVAL_THRESHOLD", DEFAULT_THRESHOLD)
    if window_days is None:
        window_days = _env_int("PAIR_WINDOW_DAYS", DEFAULT_WINDOW_DAYS)

    try:
        rows = store.query(
            "corrections",
            {
                "tenant_id": tenant_id,
                "status": "approved",
                "invalid_at": "",
                "type": "",
            },
            order_by="updated_at DESC",
            limit=WINDOW_FETCH_CAP,
        )
    except Exception as exc:
        logger.warning(f"[ApprovalPatterns] corrections query failed (ignored): {exc}")
        return []

    cutoff = (datetime.now() - timedelta(days=window_days)).isoformat()
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        if row.get("updated_at", "") < cutoff:
            continue
        creator = row.get("created_by", "")
        approver = row.get("approved_by", "")
        if not creator or not approver or creator == approver:
            continue
        key = (creator, approver)  # directed on purpose -- do not sort
        counts[key] = counts.get(key, 0) + 1

    findings = []
    for (creator, approver), count in counts.items():
        if count <= threshold:
            continue
        severity = (
            "error" if count > threshold * ESCALATION_MULTIPLIER else "warning"
        )
        finding = {
            "kind": "approval_pair_dominance",
            "proposer": creator,
            "approver": approver,
            "count": count,
            "threshold": threshold,
            "window_days": window_days,
            "severity": severity,
        }
        logger.warning(
            f"[ApprovalPatterns] Directed pair {creator}->{approver} approved "
            f"{count} corrections in {window_days}d (threshold {threshold})"
        )
        insert_flag_once(
            store,
            FLAG_TYPE_PAIR,
            subject_id=f"{creator}->{approver}",
            tenant_id=tenant_id,
            detail=finding,
            severity=severity,
        )
        findings.append(finding)
    return findings
