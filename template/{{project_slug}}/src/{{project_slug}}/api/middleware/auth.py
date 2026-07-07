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

Multi-tenancy:
    verify_api_key returns an AuthContext (not a raw string). Routes receive
    tenant_id, user_id, and the API key in a structured object. Single-tenant
    deployments use the defaults ("default" tenant, "anon" user) transparently.

    The resolved AuthContext is also stored on request.state.auth_context so
    middleware that runs OUTSIDE the dependency graph (e.g. activity
    tracking) attributes events to the same tenant/user the routes see.
    Deployments that replace verify_api_key with their own IdP integration
    (see docs/PLATFORM_GUIDE.md) keep that propagation for free as long as
    they go through this function; set MULTI_TENANT_AUTH_ENABLED=true in
    such deployments so the scaffold can warn when attribution silently
    falls back to the "default" tenant (a misconfiguration signal).
"""

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

security_scheme = HTTPBearer(auto_error=False)


@dataclass
class AuthContext:
    """Authentication context propagated to all routes.

    Extensible for multi-tenancy: add roles, permissions, org_id as needed.
    Single-tenant deployments use the defaults and ignore this structure.

    Attributes:
        api_key: The raw API key (None if auth is disabled).
        user_id: Short identifier derived from SHA-256 hash of the key (16 hex chars) or "anon".
        tenant_id: Tenant/org identifier. Defaults to "default" for single-tenant.
    """

    api_key: str | None = None
    user_id: str = "anon"
    tenant_id: str = "default"


def auth_scope_key(auth: AuthContext) -> str:
    """Return a delimiter-safe key for data owned by an auth context."""
    return json.dumps(
        [auth.tenant_id, auth.user_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )


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


def multi_tenant_auth_enabled() -> bool:
    """True when the operator declares multi-tenant auth is wired in.

    The scaffold itself never resolves a non-default tenant (that requires
    replacing verify_api_key with an IdP integration); this flag is the
    operator's declaration of that fact, and it powers misconfiguration
    warnings only -- it never changes enforcement.
    """
    return os.environ.get("MULTI_TENANT_AUTH_ENABLED", "").strip().lower() in (
        "true", "1", "yes",
    )


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
) -> AuthContext:
    """
    Verify the API key from the Authorization header.

    Returns an AuthContext with user_id and tenant_id for downstream use,
    and mirrors it onto request.state.auth_context for middleware (activity
    tracking) that runs outside the dependency graph.
    Uses constant-time comparison to prevent timing attacks.
    If API_KEY is not set, auth is disabled (dev mode only).
    """
    expected_key = get_api_key()

    if expected_key is None:
        context = AuthContext()
        request.state.auth_context = context
        return context

    if credentials is None:
        client_host = request.client.host if request.client else "unknown"
        logger.warning(f"[Auth] Missing credentials from {client_host}")
        raise HTTPException(status_code=401, detail="Missing API key")

    if not hmac.compare_digest(credentials.credentials, expected_key):
        client_host = request.client.host if request.client else "unknown"
        logger.warning(f"[Auth] Invalid API key from {client_host}")
        raise HTTPException(status_code=403, detail="Invalid API key")

    key = credentials.credentials
    user_hash = hashlib.sha256(key.encode()).hexdigest()[:16] if key else "anon"
    context = AuthContext(
        api_key=key,
        user_id=user_hash,
        tenant_id="default",
    )
    request.state.auth_context = context
    return context
