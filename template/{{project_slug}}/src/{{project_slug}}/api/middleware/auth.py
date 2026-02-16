"""
API key authentication middleware.

Simple bearer token validation. Extensible to OAuth/JWT for enterprise.

Usage:
    Set API_KEY in .env:  API_KEY=your-secret-key
    Clients pass:         Authorization: Bearer your-secret-key

Security:
    - In production (ENV=production), API_KEY is REQUIRED. Startup will FAIL
      if it's missing. Set AUTH_DISABLED=true to explicitly opt out.
    - In development (default), auth is optional for convenience.
    - API key comparison uses constant-time hmac.compare_digest (no timing attack).
"""

import hmac
import logging
import os

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

security_scheme = HTTPBearer(auto_error=False)


def get_api_key() -> str | None:
    """Load API key from environment. Returns None if auth is disabled."""
    return os.environ.get("API_KEY", "").strip() or None


def _is_production() -> bool:
    """Check if running in production mode."""
    env = os.environ.get("ENV", os.environ.get("ENVIRONMENT", "development"))
    return env.lower() in ("production", "prod", "staging")


def _auth_explicitly_disabled() -> bool:
    """Check if auth is explicitly disabled (not just missing)."""
    return os.environ.get("AUTH_DISABLED", "").lower() in ("true", "1", "yes")


def check_production_auth() -> None:
    """
    Call on startup to verify auth is configured in production.

    In production mode:
      - RAISES RuntimeError if API_KEY is not set (blocks startup)
      - Unless AUTH_DISABLED=true is explicitly set (opt-in, logged as warning)
    In development mode:
      - Logs a warning if API_KEY is not set, but allows startup
    """
    if _is_production():
        if get_api_key() is None:
            if _auth_explicitly_disabled():
                logger.warning(
                    "[Auth] AUTH_DISABLED=true in production. "
                    "All endpoints are unauthenticated. This is a security risk."
                )
            else:
                raise RuntimeError(
                    "API_KEY is required in production mode. "
                    "Set API_KEY in .env or environment variables. "
                    "To explicitly disable auth, set AUTH_DISABLED=true "
                    "(not recommended for production)."
                )
    elif get_api_key() is None:
        logger.info(
            "[Auth] No API_KEY set (dev mode). Endpoints are unauthenticated."
        )


async def verify_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(security_scheme),
) -> str | None:
    """
    Verify the API key from the Authorization header.

    Uses constant-time comparison to prevent timing attacks.
    If API_KEY is not set, auth is disabled (dev mode only).
    """
    expected_key = get_api_key()

    if expected_key is None:
        return None

    if credentials is None:
        logger.warning(f"[Auth] Missing credentials from {request.client.host}")
        raise HTTPException(status_code=401, detail="Missing API key")

    if not hmac.compare_digest(credentials.credentials, expected_key):
        logger.warning(f"[Auth] Invalid API key from {request.client.host}")
        raise HTTPException(status_code=403, detail="Invalid API key")

    return credentials.credentials
