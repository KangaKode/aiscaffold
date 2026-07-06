"""
Timing-regularity detector -- machine-like request cadence.

Humans are irregular; scripts are not. Over a user's recent
activity_events, this module computes the coefficient of variation
(CV = stddev / mean) of the inter-request intervals. A suspiciously LOW
CV means the requests arrive on a highly regular, machine-like cadence
(e.g. a scraper on a fixed timer) and is flagged for human review --
never acted on automatically.

Guards, in order:
  - health/docs/metrics routes are excluded from the interval series
    (monitors legitimately poll on a timer);
  - fewer than TIMING_MIN_SAMPLES intervals (default 20) -> no verdict;
  - zero-length intervals (concurrent requests) are tolerated as 0.0;
  - a near-zero mean interval (everything at once) -> no verdict, so
    there is no division by zero.

Known false positives: cron jobs, dashboards, and uptime monitors that
call authenticated endpoints on a schedule look exactly like this. And
the check is jitter-evadable -- an adversary who randomizes delays will
not trip it. It raises the bar; it is not proof.

Findings persist as integrity_flags (flag_type="timing_regularity_anomaly")
with a persistence-level cooldown (see learning/flags.py).

Env tunables: TIMING_MIN_SAMPLES (default 20),
TIMING_CV_THRESHOLD (default 0.1).

Leaf module: imports stdlib + learning.flags only; the store is passed in.

Keep this file under 200 lines.
"""

import logging
import os
from datetime import datetime
from statistics import pstdev

from .flags import insert_flag_once

logger = logging.getLogger(__name__)

FLAG_TYPE_TIMING = "timing_regularity_anomaly"

# Routes excluded from the interval series: unauthenticated/operational
# endpoints that monitors poll on a legitimate fixed cadence.
EXCLUDED_ROUTE_PREFIXES = ("/health", "/metrics", "/docs", "/redoc", "/openapi")

# Max rows fetched when computing the window (store supports equality
# filters only; the window is filtered in Python -- same tradeoff as
# learning/activity.py).
WINDOW_FETCH_CAP = 500

# Mean intervals at or below this (seconds) yield no verdict.
EPSILON_SECONDS = 1e-6

DEFAULT_MIN_SAMPLES = 20
DEFAULT_CV_THRESHOLD = 0.1


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def compute_interval_cv(intervals: list[float]) -> float | None:
    """
    Coefficient of variation (population stddev / mean) of the interval
    series. Returns None when the series is empty or the mean is at or
    below EPSILON_SECONDS (no meaningful cadence -- avoids divide-by-zero
    on bursts of concurrent requests).
    """
    if not intervals:
        return None
    mean = sum(intervals) / len(intervals)
    if mean <= EPSILON_SECONDS:
        return None
    return pstdev(intervals) / mean


def _interval_series(rows: list[dict]) -> list[float]:
    """Sorted inter-request intervals (seconds) from activity_events rows,
    excluding operational routes. Unparseable timestamps are skipped;
    zero-length intervals (concurrent requests) are kept as 0.0."""
    timestamps: list[datetime] = []
    for row in rows:
        route = row.get("route", "")
        if any(route.startswith(p) for p in EXCLUDED_ROUTE_PREFIXES):
            continue
        try:
            timestamps.append(datetime.fromisoformat(row.get("created_at", "")))
        except (ValueError, TypeError):
            continue
    timestamps.sort()
    return [
        (later - earlier).total_seconds()
        for earlier, later in zip(timestamps, timestamps[1:])
    ]


def check_timing_regularity(
    store,
    user_id: str,
    tenant_id: str = "default",
    min_samples: int | None = None,
    cv_threshold: float | None = None,
) -> dict | None:
    """
    Check one user's recent request cadence for machine-like regularity.

    Fetches the most recent WINDOW_FETCH_CAP activity_events for the
    user, builds the inter-request interval series (excluded routes
    dropped), and computes the CV. A CV BELOW cv_threshold with at least
    min_samples intervals is flagged.

    Returns the finding dict (also persisted as an integrity flag with
    cooldown dedupe) or None. Fire-and-forget safe: query errors are
    logged and yield None.
    """
    if min_samples is None:
        min_samples = _env_int("TIMING_MIN_SAMPLES", DEFAULT_MIN_SAMPLES)
    if cv_threshold is None:
        cv_threshold = _env_float("TIMING_CV_THRESHOLD", DEFAULT_CV_THRESHOLD)

    try:
        rows = store.query(
            "activity_events",
            {"tenant_id": tenant_id, "user_id": user_id},
            order_by="created_at DESC",
            limit=WINDOW_FETCH_CAP,
        )
    except Exception as exc:
        logger.warning(f"[Timing] activity query failed (ignored): {exc}")
        return None

    intervals = _interval_series(rows)
    if len(intervals) < min_samples:
        return None

    cv = compute_interval_cv(intervals)
    if cv is None or cv >= cv_threshold:
        return None

    finding = {
        "kind": "timing_regularity",
        "cv": round(cv, 6),
        "threshold": cv_threshold,
        "intervals": len(intervals),
        "mean_interval_seconds": round(sum(intervals) / len(intervals), 3),
    }
    logger.warning(
        f"[Timing] User '{user_id}' request cadence is machine-regular: {finding}"
    )
    insert_flag_once(
        store,
        FLAG_TYPE_TIMING,
        subject_id=user_id,
        tenant_id=tenant_id,
        detail=finding,
    )
    return finding
