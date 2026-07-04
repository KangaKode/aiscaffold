"""
Activity-tracking middleware -- records one activity_events row per request.

Registered by the gateway when ACTIVITY_TRACKING_ENABLED (default "true")
and the learning store is available. The middleware itself is a no-op
whenever app.state.activity_tracker is missing, so the app works
identically with tracking disabled or the store unavailable.

The user is attributed the same way auth does it: a 16-hex-char SHA-256
prefix of the bearer token (or "anon"). The raw key is never stored.

Recording is fire-and-forget: ActivityTracker.record() swallows storage
errors, and this middleware additionally guards the whole hook so
tracking can never fail a request.
"""

import hashlib
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


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

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        try:
            tracker = getattr(request.app.state, "activity_tracker", None)
            if tracker is not None:
                tracker.record(
                    route=request.url.path,
                    method=request.method,
                    status_code=response.status_code,
                    user_id=_user_id_from_request(request),
                )
        except Exception as exc:
            logger.warning(f"[ActivityMiddleware] recording failed (ignored): {exc}")
        return response
