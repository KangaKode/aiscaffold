"""
Trust-loop hardening -- read-time adjustments + detect-only burst flags.

Hardens the template's only closed learning loop (feedback -> trust EMA
-> routing weight) against trust/reputation poisoning. Three opt-in
mechanisms, all default OFF, none ever mutating the stored trust_score
(rollback = unset the flag; nothing is persisted):

  TRUST_DECAY_ENABLED           -- read-time decay toward neutral 0.5 from
    now - last_updated (half-life TRUST_DECAY_HALF_LIFE_DAYS, default 30).
    Double-edged by design: justified distrust is rehabilitated at the
    same rate as stale distrust (see GOVERNANCE).
  TRUST_MIN_INTERACTIONS=N      -- below N scored interactions an agent
    reports neutral 0.5 for routing, in BOTH directions (fabricated early
    praise AND earned early distrust are masked below the gate).
  TRUST_BURST_DETECTION_ENABLED -- detect-only: positive-signal bursts /
    single-source domination write integrity_flags rows (+ best-effort
    check-in). Routing is NEVER altered.

POSITIVE SIGNAL: the EMA treats a rate signal's caller-supplied
confidence as an EMA target up to 1.0 -- stronger than accept's 0.9
(agent_trust.py) -- so counting only accepts is bypassed by a rate-only
campaign. Burst and domination count accept + rate at confidence >=
POSITIVE_RATE_CONFIDENCE.

Cross-stack seam: burst detection READS the legacy SQLite stack (the
tracker passed in) and WRITES flags to the portable store; the flag
row's tenant_id defaults to project_id (the legacy stack has no tenant
concept). No cross-store atomicity -- the flag is detect-only; a lost
flag degrades observability, never correctness. The whole burst path is
fire-and-forget in both directions: a failure on either stack is a
logged no-op, never a request failure.

Gateway auto-wiring lives on the feedback route; library users call
check_feedback_burst / effective_trust_scores directly.

Keep this file under 250 lines.
"""

import logging
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..observability.metrics import record_trust_event
from .agent_trust import DEFAULT_TRUST
from .flags import record_flag_hit
from .models import SignalType

logger = logging.getLogger(__name__)

TRUST_DECAY_ENV = "TRUST_DECAY_ENABLED"
TRUST_DECAY_HALF_LIFE_ENV = "TRUST_DECAY_HALF_LIFE_DAYS"
TRUST_MIN_INTERACTIONS_ENV = "TRUST_MIN_INTERACTIONS"
TRUST_BURST_ENV = "TRUST_BURST_DETECTION_ENABLED"
DEFAULT_HALF_LIFE_DAYS = 30.0
BURST_WINDOW_MINUTES = 10
BURST_POSITIVE_THRESHOLD = 10
DOMINATION_MIN_SIGNALS = 10
DOMINATION_SHARE = 0.8
SIGNAL_SCAN_LIMIT = 50
POSITIVE_RATE_CONFIDENCE = 0.7

BURST_FLAG_TYPE = "trust_positive_burst"
DOMINATION_FLAG_TYPE = "trust_single_source_domination"
_TRUTHY = ("true", "1", "yes")


@dataclass(frozen=True)
class TrustFlags:
    """Resolved TRUST_* configuration (all off in the shipped default)."""

    decay_enabled: bool = False
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS
    min_interactions: int = 0
    burst_enabled: bool = False


def _truthy(var: str) -> bool:
    return os.environ.get(var, "").strip().lower() in _TRUTHY


def resolve_trust_flags() -> TrustFlags:
    """Parse the TRUST_* env flags; garbage numerics fall back to off /
    the documented default (mirrors the detection-hook truthy set)."""
    try:
        min_interactions = int(os.environ.get(TRUST_MIN_INTERACTIONS_ENV, "0"))
    except ValueError:
        min_interactions = 0
    try:
        half_life = float(
            os.environ.get(TRUST_DECAY_HALF_LIFE_ENV, str(DEFAULT_HALF_LIFE_DAYS))
        )
    except ValueError:
        half_life = DEFAULT_HALF_LIFE_DAYS
    if half_life <= 0:
        half_life = DEFAULT_HALF_LIFE_DAYS
    return TrustFlags(
        decay_enabled=_truthy(TRUST_DECAY_ENV),
        half_life_days=half_life,
        min_interactions=max(0, min_interactions),
        burst_enabled=_truthy(TRUST_BURST_ENV),
    )


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def decayed_score(
    raw: float, last_updated_iso: str, half_life_days: float, now: datetime | None = None
) -> float:
    """Read-time decay toward neutral: 0.5 + (raw - 0.5) * 2^(-age/hl).
    Unparseable/empty timestamps fail toward NO adjustment (return raw)."""
    updated = _parse_ts(last_updated_iso)
    if updated is None:
        return raw
    now = now or datetime.now()
    age_days = max(0.0, (now - updated).total_seconds() / 86400.0)
    return 0.5 + (raw - 0.5) * 2 ** (-age_days / half_life_days)


def effective_trust_scores(trust_mgr, project_id: str = "default") -> dict[str, float]:
    """Trust scores as routing should see them. With every TRUST_* flag
    unset this is an exact get_all_scores() passthrough (the entries read
    is never touched); with decay/gate on, adjustments are derived at
    read time and stored scores are never mutated."""
    flags = resolve_trust_flags()
    if not flags.decay_enabled and flags.min_interactions <= 0:
        return trust_mgr.get_all_scores(project_id)
    scores: dict[str, float] = {}
    for entry in trust_mgr.get_all_entries(project_id):
        if not entry.agent_id:
            continue
        if flags.min_interactions > 0 and entry.interaction_count < flags.min_interactions:
            scores[entry.agent_id] = DEFAULT_TRUST
            continue
        score = entry.trust_score
        if flags.decay_enabled:
            score = decayed_score(score, entry.last_updated, flags.half_life_days)
        scores[entry.agent_id] = score
    return scores


def _is_positive(signal) -> bool:
    if signal.signal_type == SignalType.ACCEPT:
        return True
    return (
        signal.signal_type == SignalType.RATE
        and (signal.confidence or 0.0) >= POSITIVE_RATE_CONFIDENCE
    )


def check_feedback_burst(
    store,
    tracker,
    agent_id: str,
    project_id: str = "default",
    tenant_id: str | None = None,
    checkin_manager=None,
    now: datetime | None = None,
) -> None:
    """Detect-only positive-signal burst / single-source domination scan
    for one agent (bounded: last SIGNAL_SCAN_LIMIT signals). Opt-in via
    TRUST_BURST_DETECTION_ENABLED; fire-and-forget on BOTH stacks --
    any failure is a logged no-op, never a request failure."""
    if not resolve_trust_flags().burst_enabled or not agent_id:
        return
    if store is None:
        logger.warning(
            "[TrustGuard] %s=true but no learning store -- burst detection "
            "degrades to a no-op", TRUST_BURST_ENV,
        )
        return
    try:
        tenant = tenant_id or project_id
        now = now or datetime.now()
        signals = tracker.get_signals(
            project_id=project_id, agent_id=agent_id, limit=SIGNAL_SCAN_LIMIT
        )
        positives = [s for s in signals if _is_positive(s)]

        window_start = now - timedelta(minutes=BURST_WINDOW_MINUTES)
        in_window = [
            s for s in positives
            if (ts := _parse_ts(s.created_at)) is not None and ts >= window_start
        ]
        if len(in_window) >= BURST_POSITIVE_THRESHOLD:
            # Detail carries counts/timestamps only -- never feedback
            # content or session ids (both can be user-supplied text).
            _flag(
                store, BURST_FLAG_TYPE, agent_id, tenant, checkin_manager,
                project_id, "burst_flagged",
                {
                    "positives_in_window": len(in_window),
                    "window_minutes": BURST_WINDOW_MINUTES,
                    "threshold": BURST_POSITIVE_THRESHOLD,
                },
            )

        with_session = [s for s in positives if s.session_id]
        if len(with_session) >= DOMINATION_MIN_SIGNALS:
            top = Counter(s.session_id for s in with_session).most_common(1)[0][1]
            share = top / len(with_session)
            if share >= DOMINATION_SHARE:
                _flag(
                    store, DOMINATION_FLAG_TYPE, agent_id, tenant, checkin_manager,
                    project_id, "domination_flagged",
                    {
                        "top_session_share": round(share, 3),
                        "positives_with_session": len(with_session),
                        "distinct_sessions": len(set(s.session_id for s in with_session)),
                    },
                )
    except Exception as exc:
        logger.warning(f"[TrustGuard] burst check failed (non-fatal): {exc}")


def _flag(
    store, flag_type, agent_id, tenant, checkin_manager, project_id, action, detail
) -> None:
    """Insert-or-update the flag; on a NEW insert, record the metric and
    raise a best-effort check-in (collusion-detector precedent)."""
    if not record_flag_hit(store, flag_type, agent_id, tenant, detail):
        return
    record_trust_event(action)
    if checkin_manager is None:
        return
    try:
        checkin_manager.create(
            checkin_type="trust_review",
            prompt=(
                f"Agent '{agent_id}' triggered {flag_type} {detail}. Review "
                "its recent feedback stream for trust poisoning."
            ),
            suggested_action=(
                "If manipulation is confirmed, resolve the integrity flag and "
                "consider suspending the feedback source or the agent."
            ),
            project_id=project_id,
            context=detail,
        )
        record_trust_event("checkin_created")
    except Exception as exc:
        logger.warning(f"[TrustGuard] check-in creation failed (non-fatal): {exc}")
