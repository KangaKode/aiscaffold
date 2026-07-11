"""
Activity analytics -- user request patterns and agent behavior baselines.

Two trackers over the LearningStore:

  ActivityTracker      -- records API activity (activity_events) and flags
                          bursts: too many requests, repeated auth
                          failures, or agent-registration sprees.
  AgentBaselineTracker -- records per-dispatch agent stats
                          (agent_dispatch_stats) and flags agents whose
                          recent behavior deviates from their own history.

Identity says WHO the agent is; baselines say whether it still BEHAVES
like itself. A compromised or drifting agent keeps valid credentials --
what changes is its refusal rate, confidence, latency, or scope
discipline.

Time-window queries: the store protocol only supports equality filters,
so rolling windows are computed by reading the most recent N rows
(ordered by created_at DESC, N capped) and filtering by timestamp in
Python. Tradeoff: bounded extra reads and a hard cap on how far back a
window can see, in exchange for keeping the storage protocol tiny and
backend-portable. Deployments with heavy traffic can raise the caps or
implement a store with native range queries.

Anomalies are persisted as integrity_flags and surfaced through
GET /api/v1/activity/anomalies -- never acted on automatically.

Keep this file under 350 lines.
"""

import logging
import os
import uuid
from datetime import datetime, timedelta

from .flags import insert_flag_once

logger = logging.getLogger(__name__)

FLAG_TYPE_USER_ANOMALY = "user_activity_anomaly"
FLAG_TYPE_AGENT_ANOMALY = "agent_behavior_anomaly"

DEFAULT_THRESHOLDS = {
    "requests_per_hour": 300,
    "failed_auth_per_hour": 10,
    "agent_registrations_per_day": 20,
}

# Max rows fetched when computing a rolling window (see module docstring).
WINDOW_FETCH_CAP = 2000

# Status codes counted as authentication failures.
AUTH_FAILURE_CODES = (401, 403)

# Ops/infra routes excluded from the requests_per_hour burst count:
# liveness/readiness pollers and metric scrapers legitimately hit these
# on tight schedules (a 10s liveness probe alone is 360 req/h) and would
# otherwise drown the 300/h default threshold in infrastructure noise.
# They still count toward failed-auth (a 401 on /metrics is meaningful)
# and are still recorded as activity_events.
OPS_ROUTES = ("/health", "/health/ready", "/metrics", "/metrics/prometheus")


def _now_iso() -> str:
    return datetime.now().isoformat()


class ActivityTracker:
    """
    Records API activity and checks per-user thresholds.

    Usage:
        tracker = ActivityTracker(get_learning_store())
        tracker.record("/api/v1/agents", "POST", 200, user_id="abc123")
        flags = tracker.check_thresholds("abc123")
    """

    def __init__(self, store):
        self._store = store

    def record(
        self,
        route: str,
        method: str,
        status_code: int,
        user_id: str = "",
        tenant_id: str = "default",
    ) -> None:
        """
        Record one API request. Fire-and-forget: any storage error is
        logged and swallowed so tracking can never break request handling.
        """
        try:
            self._store.insert(
                "activity_events",
                {
                    "id": str(uuid.uuid4())[:12],
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "route": route,
                    "method": method,
                    "status_code": int(status_code),
                    "created_at": _now_iso(),
                },
            )
        except Exception as exc:
            logger.warning(f"[Activity] record() failed (ignored): {exc}")

    def check_thresholds(
        self,
        user_id: str,
        tenant_id: str = "default",
        thresholds: dict | None = None,
    ) -> list[str]:
        """
        Check a user's recent activity against burst thresholds.

        Defaults: requests_per_hour=300, failed_auth_per_hour=10,
        agent_registrations_per_day=20 (override any subset via the
        thresholds dict). Windows are rolling, computed by fetching the
        most recent rows and filtering by timestamp in Python (see module
        docstring for the tradeoff). Ops/infra routes (OPS_ROUTES) are
        excluded from the requests_per_hour count so health probes and
        metric scrapes cannot trip the burst flag.

        Breaches are persisted as integrity_flags
        (flag_type="user_activity_anomaly", subject_id=user_id) with a
        cooldown -- a sustained burst yields one unresolved flag, not one
        per check (see learning/flags.py) -- and returned as
        human-readable strings.
        """
        limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        try:
            rows = self._store.query(
                "activity_events",
                {"tenant_id": tenant_id, "user_id": user_id},
                order_by="created_at DESC",
                limit=WINDOW_FETCH_CAP,
            )
        except Exception as exc:
            logger.warning(f"[Activity] check_thresholds query failed: {exc}")
            return []

        # Naive local time, like the stored timestamps: a DST shift can
        # skew a window by an hour. Harmless for burst heuristics.
        now = datetime.now()
        hour_cutoff = (now - timedelta(hours=1)).isoformat()
        day_cutoff = (now - timedelta(days=1)).isoformat()

        last_hour = [r for r in rows if r.get("created_at", "") >= hour_cutoff]
        last_day = [r for r in rows if r.get("created_at", "") >= day_cutoff]

        requests_hour = sum(
            1 for r in last_hour if r.get("route", "") not in OPS_ROUTES
        )
        failed_auth_hour = sum(
            1 for r in last_hour if r.get("status_code") in AUTH_FAILURE_CODES
        )
        registrations_day = sum(
            1
            for r in last_day
            if r.get("method", "").upper() == "POST"
            and r.get("route", "").rstrip("/").endswith("/agents")
        )

        flags: list[str] = []
        observed = {
            "requests_per_hour": requests_hour,
            "failed_auth_per_hour": failed_auth_hour,
            "agent_registrations_per_day": registrations_day,
        }
        for name, value in observed.items():
            limit = limits[name]
            if value > limit:
                flags.append(f"{name}: {value} > {limit}")

        if flags:
            logger.warning(
                f"[Activity] User '{user_id}' anomaly: {flags}"
            )
            insert_flag_once(
                self._store,
                FLAG_TYPE_USER_ANOMALY,
                subject_id=user_id,
                tenant_id=tenant_id,
                detail={"flags": flags, "observed": observed, "limits": limits},
            )
        return flags


class AgentBaselineTracker:
    """
    Tracks per-agent dispatch statistics and flags behavioral drift.

    Identity verification says WHO an agent is; this tracker says whether
    it still BEHAVES like itself. Baselines are simple per-metric means
    over the agent's own history (duration, refusal rate, confidence,
    scope violations); deviation beyond a relative tolerance is flagged
    for human review -- never acted on automatically.
    """

    METRICS = ("duration_seconds", "refusal_rate", "confidence", "scope_violations")

    def __init__(self, store):
        self._store = store

    def record_dispatch(
        self,
        agent_id: str,
        duration_seconds: float,
        refused: bool,
        confidence: float,
        scope_violations: int,
        tenant_id: str = "default",
    ) -> None:
        """Record one dispatch. Fire-and-forget (errors logged, not raised)."""
        try:
            self._store.insert(
                "agent_dispatch_stats",
                {
                    "id": str(uuid.uuid4())[:12],
                    "agent_id": agent_id,
                    "tenant_id": tenant_id,
                    "dispatched_at": _now_iso(),
                    "duration_seconds": float(duration_seconds),
                    "refused": 1 if refused else 0,
                    "confidence": float(confidence),
                    "scope_violations": int(scope_violations),
                },
            )
        except Exception as exc:
            logger.warning(f"[Activity] record_dispatch() failed (ignored): {exc}")

    def compute_baseline(
        self, agent_id: str, min_samples: int = 20, tenant_id: str = "default"
    ) -> dict | None:
        """
        Per-metric means over the agent's recorded history IN ONE TENANT
        (capped at the most recent WINDOW_FETCH_CAP dispatches). Returns
        None when fewer than min_samples dispatches exist. Tenant scoping
        matters because the same (possibly public) agent name can be
        dispatched by several tenants -- a blended baseline would compare
        an agent against other tenants' workloads.

        Keys: duration_seconds, refusal_rate, confidence, scope_violations,
        plus "samples".
        """
        rows = self._fetch(agent_id, tenant_id)
        if len(rows) < min_samples:
            return None
        return self._means(rows)

    def check_deviation(
        self,
        agent_id: str,
        recent_window: int = 10,
        tolerance: float = 0.5,
        tenant_id: str = "default",
    ) -> list[str]:
        """
        Compare recent-window means against the agent's own baseline,
        scoped to one tenant (see compute_baseline for why).

        Baseline = means over history EXCLUDING the recent window (so a
        shift isn't diluted by its own samples); requires >= 20 baseline
        samples. Relative deviation |recent - base| / base > tolerance is
        flagged; when a baseline mean is ~0 the absolute difference is
        compared against tolerance directly (documented v1 behavior).

        Flags are persisted as integrity_flags
        (flag_type="agent_behavior_anomaly", subject_id=agent_id) in the
        queried tenant and returned as human-readable strings.
        """
        rows = self._fetch(agent_id, tenant_id)
        recent_rows = rows[:recent_window]
        baseline_rows = rows[recent_window:]
        if len(recent_rows) < recent_window or len(baseline_rows) < 20:
            return []

        recent = self._means(recent_rows)
        baseline = self._means(baseline_rows)

        flags: list[str] = []
        for metric in self.METRICS:
            base, cur = baseline[metric], recent[metric]
            if abs(base) > 1e-9:
                deviation = abs(cur - base) / abs(base)
            else:
                deviation = abs(cur - base)
            if deviation > tolerance:
                flags.append(
                    f"{metric}: recent mean {cur:.3f} vs baseline {base:.3f} "
                    f"(deviation {deviation:.2f} > {tolerance})"
                )

        if flags:
            logger.warning(f"[Activity] Agent '{agent_id}' behavior drift: {flags}")
            insert_flag_once(
                self._store,
                FLAG_TYPE_AGENT_ANOMALY,
                subject_id=agent_id,
                tenant_id=tenant_id,
                detail={"flags": flags, "recent": recent, "baseline": baseline},
            )
        return flags

    def _fetch(self, agent_id: str, tenant_id: str = "default") -> list[dict]:
        try:
            return self._store.query(
                "agent_dispatch_stats",
                {"agent_id": agent_id, "tenant_id": tenant_id},
                order_by="dispatched_at DESC",
                limit=WINDOW_FETCH_CAP,
            )
        except Exception as exc:
            logger.warning(f"[Activity] dispatch stats query failed: {exc}")
            return []

    @staticmethod
    def _means(rows: list[dict]) -> dict:
        n = len(rows)
        return {
            "duration_seconds": sum(r.get("duration_seconds") or 0.0 for r in rows) / n,
            "refusal_rate": sum(1 for r in rows if r.get("refused")) / n,
            "confidence": sum(r.get("confidence") or 0.0 for r in rows) / n,
            "scope_violations": sum(r.get("scope_violations") or 0 for r in rows) / n,
            "samples": n,
        }


def create_baseline_tracker(store) -> AgentBaselineTracker | None:
    """Build a tracker when BASELINE_TRACKING_ENABLED is truthy, else None.

    Opt-in wiring (default OFF): returning None keeps every dispatch path
    byte-identical to the untracked behavior. Detection-only -- recorded
    stats and deviation flags are never acted on automatically.
    """
    if os.environ.get("BASELINE_TRACKING_ENABLED", "").strip().lower() not in (
        "true", "1", "yes",
    ):
        return None
    if store is None:
        logger.warning(
            "[Activity] BASELINE_TRACKING_ENABLED=true but no learning store "
            "is available -- baseline tracking degrades to a no-op"
        )
        return None
    return AgentBaselineTracker(store)
