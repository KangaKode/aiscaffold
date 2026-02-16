"""
Rate limiting middleware -- prevents abuse from any single client.

Uses a simple in-memory sliding window counter per client IP.
For production with multiple replicas, replace with Redis-backed limiter.

Configuration via environment:
  RATE_LIMIT_PER_MINUTE=60  (default: 60 requests per minute per IP)
"""

import logging
import os
import time
from collections import defaultdict

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

DEFAULT_RATE_LIMIT = 60
MAX_TRACKED_IPS = 10_000
GLOBAL_CLEANUP_INTERVAL = 60.0


def _get_rate_limit() -> int:
    """Load rate limit from environment."""
    try:
        return int(os.environ.get("RATE_LIMIT_PER_MINUTE", DEFAULT_RATE_LIMIT))
    except ValueError:
        return DEFAULT_RATE_LIMIT


_request_log: dict[str, list[float]] = defaultdict(list)
_last_global_cleanup: float = 0.0


def _cleanup_old_entries(client_id: str, window_seconds: float = 60.0) -> None:
    """Remove request timestamps older than the window."""
    cutoff = time.time() - window_seconds
    _request_log[client_id] = [
        ts for ts in _request_log[client_id] if ts > cutoff
    ]


def _global_cleanup(window_seconds: float = 60.0) -> None:
    """Sweep all IPs: remove expired timestamps and delete empty entries.

    Runs at most once per GLOBAL_CLEANUP_INTERVAL seconds.
    """
    global _last_global_cleanup
    now = time.time()
    if now - _last_global_cleanup < GLOBAL_CLEANUP_INTERVAL:
        return

    _last_global_cleanup = now
    cutoff = now - window_seconds
    empty_keys = []

    for client_id in list(_request_log.keys()):
        _request_log[client_id] = [
            ts for ts in _request_log[client_id] if ts > cutoff
        ]
        if not _request_log[client_id]:
            empty_keys.append(client_id)

    for key in empty_keys:
        del _request_log[key]

    if empty_keys:
        logger.debug(f"[RateLimit] Global cleanup: evicted {len(empty_keys)} stale IPs")


async def check_rate_limit(request: Request) -> None:
    """
    Check if the client has exceeded the rate limit.

    Call this as a dependency in routes that need rate limiting.
    Raises HTTP 429 if the limit is exceeded.
    Raises HTTP 503 if the IP tracking table is full.
    """
    client_ip = request.client.host if request.client else "unknown"
    limit = _get_rate_limit()

    _global_cleanup()

    if len(_request_log) >= MAX_TRACKED_IPS and client_ip not in _request_log:
        logger.warning(
            f"[RateLimit] IP tracking table full ({MAX_TRACKED_IPS}). "
            f"Rejecting new client {client_ip}"
        )
        raise HTTPException(
            status_code=503,
            detail="Server is under heavy load. Try again later.",
            headers={"Retry-After": "60"},
        )

    _cleanup_old_entries(client_ip)

    if len(_request_log[client_ip]) >= limit:
        logger.warning(f"[RateLimit] Client {client_ip} exceeded {limit}/min")
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({limit} requests per minute)",
            headers={"Retry-After": "60"},
        )

    _request_log[client_ip].append(time.time())
