"""
Activity-tracking middleware -- records one activity_events row per request.

Registered by the gateway when ACTIVITY_TRACKING_ENABLED (default "true")
and the learning store is available. The middleware itself is a no-op
whenever app.state.activity_tracker is missing, so the app works
identically with tracking disabled or the store unavailable.

The user is attributed the same way auth does it: a 16-hex-char SHA-256
prefix of the bearer token (or "anon"). The raw key is never stored.

Every Nth recorded request (ACTIVITY_CHECK_SAMPLE_N, default 25) the
middleware also runs the detection pass for that user: the burst
thresholds (ActivityTracker.check_thresholds) and the timing-regularity
CV check (learning/timing_analysis.py). Sampling keeps the per-request
cost at zero for the other N-1 requests while anomalies still surface
within one sampling period.

Recording is fire-and-forget: ActivityTracker.record() swallows storage
errors, and this middleware additionally guards the whole hook so
tracking can never fail a request.
"""

import hashlib
import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

DEFAULT_CHECK_SAMPLE_N = 25


def _check_sample_n() -> int:
    raw = os.environ.get("ACTIVITY_CHECK_SAMPLE_N", "").strip()
    n = int(raw) if raw.isdigit() else DEFAULT_CHECK_SAMPLE_N
    return max(1, n)


def _user_id_from_request(request: Request) -> str:
    """Derive the same anonymized user id auth uses (hash prefix or 'anon')."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return hashlib.sha256(token.encode()).hexdigest()[:16]
    return "anon"


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
                user_id = _user_id_from_request(request)
                tracker.record(
                    route=request.url.path,
                    method=request.method,
                    status_code=response.status_code,
                    user_id=user_id,
                )
                self._records_since_check += 1
                if self._records_since_check >= _check_sample_n():
                    self._records_since_check = 0
                    self._run_checks(request, tracker, user_id)
        except Exception as exc:
            logger.warning(f"[ActivityMiddleware] recording failed (ignored): {exc}")
        return response

    @staticmethod
    def _run_checks(request: Request, tracker, user_id: str) -> None:
        """Sampled detection pass: burst thresholds + timing regularity.
        Best-effort -- findings persist as integrity flags; failures are
        logged and can never fail the request."""
        tracker.check_thresholds(user_id)
        store = getattr(request.app.state, "learning_store", None)
        if store is not None:
            from ...learning.timing_analysis import check_timing_regularity

            check_timing_regularity(store, user_id=user_id)
