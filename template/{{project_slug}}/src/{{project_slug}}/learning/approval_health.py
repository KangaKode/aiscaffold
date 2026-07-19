"""
Approval-health stats -- batched human-gate health check.

Pure compute over already-fetched governance-report rows. Must NEVER call
store.query (or any other store I/O). Consumed only by
learning/governance_report.build_governance_report as
sections.approval_health.

Propose→approve latency uses updated_at - created_at on currently
status=approved rows. That is valid because revalidate deliberately does
not bump updated_at, and supersession's ancestor invalidation via
update_if also leaves updated_at untouched.

Signal only: no runtime path consumes these numbers to block or alter
approvals. See GOVERNANCE Non-Claim (not a rubber-stamping detector).

Keep this file under 200 lines.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

LATENCY_SAMPLE_FLOOR = 5
FLAG_REF_TYPES = (
    "four_eyes_unenforceable",
    "approval_pair_dominance",
    "supersession_partial_failure",
)


def build_section(
    corr_rows: list[dict],
    flag_rows: list[dict],
    corr_coverage: dict,
    flag_coverage: dict,
) -> dict[str, Any]:
    """Assemble sections.approval_health from pre-fetched window rows.

    Coverage merges corrections + flags horizons: integrity_flag_refs
    come from flag_rows, so a flags-only cap hit must still set
    coverage_partial (never report refs as complete when flags truncated).
    """
    approved = [r for r in corr_rows if r.get("status") == "approved"]
    latencies = _approval_latencies(approved)
    sample_size = len(latencies)
    insufficient = sample_size < LATENCY_SAMPLE_FLOOR
    if insufficient:
        p50: float | None = None
        p95: float | None = None
    else:
        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95)

    return {
        "approval_latency_seconds_p50": p50,
        "approval_latency_seconds_p95": p95,
        "latency_sample_size": sample_size,
        "insufficient_sample": insufficient,
        "lifecycle_by_status": _count_by(corr_rows, "status"),
        "self_approval_count": _self_approval_count(approved),
        "supersession_activity_count": sum(
            1 for r in approved if r.get("supersedes_id")
        ),
        "integrity_flag_refs": _flag_refs(flag_rows),
        **_merged_coverage(corr_coverage, flag_coverage),
    }


def _merged_coverage(corr_coverage: dict, flag_coverage: dict) -> dict:
    """OR coverage_partial; keep corrections window bounds as the primary stamp."""
    return {
        "coverage_from": corr_coverage.get("coverage_from"),
        "coverage_to": corr_coverage.get("coverage_to"),
        "coverage_partial": bool(
            corr_coverage.get("coverage_partial")
            or flag_coverage.get("coverage_partial")
        ),
    }


def _approval_latencies(approved: list[dict]) -> list[float]:
    """Seconds from created_at to updated_at; skip bad/negative; keep zeros."""
    out: list[float] = []
    for row in approved:
        created = _parse(row.get("created_at") or "")
        updated = _parse(row.get("updated_at") or "")
        if created is None or updated is None:
            continue
        delta = (updated - created).total_seconds()
        if delta < 0:
            continue
        out.append(delta)
    return out


def _self_approval_count(approved: list[dict]) -> int:
    n = 0
    for row in approved:
        creator = row.get("created_by") or ""
        approver = row.get("approved_by") or ""
        if creator and approver and creator == approver:
            n += 1
    return n


def _flag_refs(flag_rows: list[dict]) -> dict[str, int]:
    counts = _count_by(flag_rows, "flag_type")
    return {key: int(counts.get(key, 0)) for key in FLAG_REF_TYPES}


def _count_by(rows: list[dict], column: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get(column) or "")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile; values must be non-empty."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return round(ordered[low] * (1.0 - frac) + ordered[high] * frac, 3)


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return None
