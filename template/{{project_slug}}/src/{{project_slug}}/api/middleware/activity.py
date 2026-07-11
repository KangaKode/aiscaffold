"""
Activity-tracking middleware -- records one activity_events row per request.

Registered by the gateway when ACTIVITY_TRACKING_ENABLED (default "true")
and the learning store is available. The middleware itself is a no-op
whenever app.state.activity_tracker is missing, so the app works
identically with tracking disabled or the store unavailable.

Attribution: the resolved AuthContext (mirrored by verify_api_key onto
request.state.auth_context) is preferred, so events carry the SAME
tenant_id and user_id the route handler saw -- baselines, anomaly flags,
and retention land in the caller's tenant. Requests that never resolve
an auth context (unauthenticated paths, failed auth) fall back to the
historical derivation: tenant "default" and a 16-hex-char SHA-256 prefix
of the bearer token (or "anon"). The raw key is never stored.

When the operator declares multi-tenant auth (MULTI_TENANT_AUTH_ENABLED
=true) but an AUTHENTICATED request (auth context present) still
resolves to the "default" tenant, a one-time warning fires --
attribution is misconfigured (e.g. a custom verify_api_key that never
sets a real tenant_id). Unauthenticated traffic (health probes, failed
auth) legitimately falls back to the default tenant and must NOT burn
the one-time warning, or a real misconfiguration on authenticated
routes would go unreported. The warning is inert: nothing is blocked
or altered.

Every Nth recorded request (ACTIVITY_CHECK_SAMPLE_N, default 25) the
middleware also runs the detection pass for that user: the burst
thresholds (ActivityTracker.check_thresholds), the timing-regularity
CV check (learning/timing_analysis.py), and -- opt-in via
SEQUENCE_DETECTION_ENABLED (default off) -- the multi-step
extraction-playbook scan (harness/sequence_detector.py), which reads
the same activity_events and writes its own integrity flags. Sampling
keeps the per-request cost at zero for the other N-1 requests while
anomalies still surface within one sampling period.

Recording is fire-and-forget: ActivityTracker.record() swallows storage
errors, and this middleware additionally guards the whole hook so
tracking can never fail a request.

The tracker's SQLite writes and the sampled detection pass are
synchronous; both run via asyncio.to_thread so they never block the
event loop under concurrent traffic.
"""

import asyncio
import hashlib
import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .auth import multi_tenant_auth_enabled

logger = logging.getLogger(__name__)

DEFAULT_CHECK_SAMPLE_N = 25

DEFAULT_TENANT = "default"

# One warning per process when multi-tenant auth is declared but events
# still land in the default tenant (misconfigured attribution).
_default_tenant_warned = False


def _check_sample_n() -> int:
    raw = os.environ.get("ACTIVITY_CHECK_SAMPLE_N", "").strip()
    n = int(raw) if raw.isdigit() else DEFAULT_CHECK_SAMPLE_N
    return max(1, n)


def _sequence_detection_enabled() -> bool:
    """Opt-in toggle for the extraction-sequence scan (default off)."""
    return os.environ.get("SEQUENCE_DETECTION_ENABLED", "").strip().lower() in (
        "true", "1", "yes",
    )


def _user_id_from_request(request: Request) -> str:
    """Derive the same anonymized user id auth uses (hash prefix or 'anon')."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return hashlib.sha256(token.encode()).hexdigest()[:16]
    return "anon"


def _attribution_from_request(request: Request) -> tuple[str, str, bool]:
    """Resolve (tenant_id, user_id, authenticated) for one recorded event.

    Prefers the AuthContext the route's verify_api_key dependency mirrored
    onto request.state; falls back to the header-derived user and the
    default tenant when no context was resolved (unauthenticated routes,
    failed auth -- an unauthenticated caller can never claim a tenant).
    The authenticated bit tells the caller whether the default tenant was
    RESOLVED by auth (misconfiguration signal) or is just the fallback.
    """
    auth = getattr(request.state, "auth_context", None)
    if auth is not None:
        return auth.tenant_id, auth.user_id, True
    return DEFAULT_TENANT, _user_id_from_request(request), False


def _warn_default_tenant_once() -> None:
    global _default_tenant_warned
    if _default_tenant_warned:
        return
    _default_tenant_warned = True
    logger.warning(
        "[ActivityMiddleware] MULTI_TENANT_AUTH_ENABLED=true but an "
        "authenticated request's activity event was recorded under the "
        "'default' tenant. Tenant attribution "
        "for behavioral baselines, anomaly flags, and retention may be "
        "misconfigured -- confirm your auth integration sets "
        "request.state.auth_context with the caller's real tenant_id. "
        "Detection-only: this request was recorded normally."
    )


class ActivityTrackingMiddleware(BaseHTTPMiddleware):
    """Records route/method/status per request via app.state.activity_tracker."""

    def __init__(self, app):
        super().__init__(app)
        self._records_since_check = 0

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        try:
            tracker = getattr(request.app.state, "activity_tracker", None)
            if tracker is not None:
                tenant_id, user_id, authenticated = _attribution_from_request(request)
                if (
                    authenticated
                    and tenant_id == DEFAULT_TENANT
                    and multi_tenant_auth_enabled()
                ):
                    _warn_default_tenant_once()
                # Blocking SQLite write: off the event loop.
                await asyncio.to_thread(
                    tracker.record,
                    route=request.url.path,
                    method=request.method,
                    status_code=response.status_code,
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
                self._records_since_check += 1
                if self._records_since_check >= _check_sample_n():
                    self._records_since_check = 0
                    await asyncio.to_thread(
                        self._run_checks, request, tracker, user_id, tenant_id
                    )
        except Exception as exc:
            logger.warning(f"[ActivityMiddleware] recording failed (ignored): {exc}")
        return response

    @staticmethod
    def _run_checks(request: Request, tracker, user_id: str, tenant_id: str) -> None:
        """Sampled detection pass: burst thresholds + timing regularity,
        the opt-in extraction-sequence scan (SEQUENCE_DETECTION_ENABLED,
        default off -- detect-only, flags multi-step playbooks that stay
        under the volume thresholds), plus the opt-in retention prune
        (ACTIVITY_RETENTION_DAYS -- a no-op unless the operator sets
        it). Best-effort -- findings persist as integrity flags;
        failures are logged and can never fail the request. Runs in the
        caller's tenant so flags land where the events did."""
        tracker.check_thresholds(user_id, tenant_id=tenant_id)
        store = getattr(request.app.state, "learning_store", None)
        if store is not None:
            from ...learning.retention import prune_activity_events
            from ...learning.timing_analysis import check_timing_regularity

            check_timing_regularity(store, user_id=user_id, tenant_id=tenant_id)
            if _sequence_detection_enabled():
                from ...harness.sequence_detector import SequenceDetector

                SequenceDetector(store).check(user_id, tenant_id=tenant_id)
            prune_activity_events(store)
