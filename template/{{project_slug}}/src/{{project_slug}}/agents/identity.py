"""Agent identity token generation, verification, and hashing.

Provides cryptographic agent identity via platform-issued JWT (HS256).
Tokens bind agent_id, tenant_id, scopes, and is_meta_agent to a signed
credential that is verified at orchestrator dispatch time.

Security properties:
  - algorithms=["HS256"] explicit in jwt.decode (prevents alg confusion)
  - Kill switch (AGENT_IDENTITY_ENABLED) acts at verification, not issuance,
    and STILL enforces token expiry when disabled
  - Dev-mode ephemeral key uses secrets.token_hex(32)
"""

import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt as pyjwt

logger = logging.getLogger(__name__)

ISSUER = "agent-platform"
AUDIENCE = "agent-dispatch"

_signing_key_cache: str | None = None


@dataclass
class AgentIdentityClaims:
    """Parsed JWT claims from a verified agent identity token."""

    agent_id: str
    tenant_id: str
    scopes: list[str]
    is_meta_agent: bool
    issued_at: datetime
    expires_at: datetime
    issuer: str


def _is_production() -> bool:
    """Check if running in production mode (same convention as api.middleware.auth)."""
    env = os.environ.get("ENV", os.environ.get("ENVIRONMENT", "development"))
    return env.lower() in ("production", "prod", "staging")


def _get_default_ttl() -> int:
    """Resolve default TTL from env or environment type.

    AGENT_DEFAULT_TTL_DAYS env var overrides. If unset or invalid:
    production/staging -> 7 days, dev/test -> 90 days.
    """
    raw = os.environ.get("AGENT_DEFAULT_TTL_DAYS")
    if raw is not None:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
        logger.warning(
            "[AgentIdentity] Invalid AGENT_DEFAULT_TTL_DAYS (non-numeric), using environment default",
        )
    return 7 if _is_production() else 90


def _get_max_ttl() -> int:
    """Resolve max TTL ceiling from env. Default 90, minimum 1.

    Invalid values fall back to 90 with a warning.
    """
    raw = os.environ.get("AGENT_MAX_TTL_DAYS")
    if raw is not None:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
        logger.warning(
            "[AgentIdentity] Invalid AGENT_MAX_TTL_DAYS (non-numeric), using default 90",
        )
    return 90


def _get_signing_key() -> str:
    """Load signing key from env, auto-generate ephemeral key in dev mode."""
    global _signing_key_cache
    if _signing_key_cache is not None:
        return _signing_key_cache

    key = os.environ.get("AGENT_IDENTITY_SIGNING_KEY", "").strip()
    if key:
        if len(key) < 64:
            if _is_production():
                raise RuntimeError(
                    f"AGENT_IDENTITY_SIGNING_KEY is too short "
                    f"(got {len(key)} chars, minimum 64 required). "
                    'Generate one: python -c "import secrets; print(secrets.token_hex(32))"'
                )
            logger.warning(
                "[AgentIdentity] Signing key is < 64 chars (got %d). Using ephemeral key instead.",
                len(key),
            )
            key = secrets.token_hex(32)
        _signing_key_cache = key
        return key

    if _is_production():
        raise RuntimeError(
            "AGENT_IDENTITY_SIGNING_KEY is required in production. "
            "Set this environment variable to a 64+ character hex string."
        )

    key = secrets.token_hex(32)
    logger.warning(
        "[AgentIdentity] Dev-mode: using ephemeral signing key. "
        "This key will not persist across restarts. "
        "Set AGENT_IDENTITY_SIGNING_KEY for production use."
    )
    _signing_key_cache = key
    return key


def _reset_signing_key() -> None:
    """Reset cached signing key (testing only)."""
    global _signing_key_cache
    _signing_key_cache = None


def issue_token(
    agent_id: str,
    tenant_id: str,
    scopes: list[str],
    is_meta_agent: bool = False,
    expires_in_days: int | None = None,
) -> tuple[str, int]:
    """Issue a JWT identity token for an agent.

    Tokens are ALWAYS issued regardless of AGENT_IDENTITY_ENABLED.
    The kill switch only affects verification, not issuance.

    Returns (token, effective_ttl_days). Callers that compute stored expiry
    must use the returned effective_ttl_days, not their raw input.
    """
    key = _get_signing_key()
    resolved_ttl = expires_in_days if expires_in_days is not None else _get_default_ttl()
    max_ttl = _get_max_ttl()
    effective_ttl = max(1, min(resolved_ttl, max_ttl))
    if resolved_ttl != effective_ttl:
        logger.warning(
            "[AgentIdentity] TTL clamped: requested %d, effective %d (max %d)",
            resolved_ttl,
            effective_ttl,
            max_ttl,
        )
    now = int(time.time())
    claims = {
        "sub": agent_id,
        "tid": tenant_id,
        "scopes": scopes,
        "meta": is_meta_agent,
        "iat": now,
        "exp": now + (effective_ttl * 86400),
        "iss": ISSUER,
        "aud": AUDIENCE,
    }
    logger.info("[AgentIdentity] Issued token for agent '%s' (ttl=%dd)", agent_id, effective_ttl)
    return pyjwt.encode(claims, key, algorithm="HS256"), effective_ttl


def token_expires_at(token: str) -> str:
    """ISO 8601 UTC expiry of an issued token, or "" if unreadable.

    Reads the exp claim WITHOUT verifying the signature: this is a
    display helper for the issuing side (register/rotate responses
    surface when the credential dies), never an authorization check --
    verification stays in verify_token.
    """
    try:
        claims = pyjwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
            },
        )
        return datetime.fromtimestamp(int(claims["exp"]), tz=UTC).isoformat()
    except Exception as exc:
        logger.warning(f"[AgentIdentity] Could not read token expiry: {exc}")
        return ""


def _check_expiry_warning(claims: AgentIdentityClaims) -> None:
    """Log a warning if the token is nearing expiry."""
    remaining = (claims.expires_at - datetime.now(UTC)).total_seconds() / 86400
    if remaining <= 0:
        return
    if remaining <= 7:
        logger.warning(
            "[AgentIdentity] Token for agent '%s' expires in %.1f days",
            claims.agent_id,
            remaining,
        )


def verify_token(token: str) -> AgentIdentityClaims | None:
    """Verify a JWT identity token and return parsed claims.

    Returns None on any failure (expired, tampered, wrong key).
    Never raises to the caller.

    Kill switch: When AGENT_IDENTITY_ENABLED=false, skips cryptographic
    verification but still decodes claims and enforces expiry. Disabling
    is refused in production.

    Algorithm restriction: Explicitly specifies algorithms=["HS256"]
    to prevent algorithm confusion attacks (alg: none).
    """
    enabled = os.environ.get("AGENT_IDENTITY_ENABLED", "true").lower() == "true"
    if not enabled:
        if _is_production():
            logger.error(
                "[AgentIdentity] AGENT_IDENTITY_ENABLED=false is not allowed in production. "
                "Proceeding with verification enabled."
            )
            enabled = True
        else:
            logger.warning(
                "[AgentIdentity] Verification disabled (AGENT_IDENTITY_ENABLED=false), "
                "skipping cryptographic check"
            )
            claims = _decode_claims_unverified(token)
            if claims is not None:
                _check_expiry_warning(claims)
            return claims

    try:
        key = _get_signing_key()
        payload = pyjwt.decode(token, key, algorithms=["HS256"], audience=AUDIENCE)
        claims = _claims_from_payload(payload)
        _check_expiry_warning(claims)
        return claims
    except pyjwt.ExpiredSignatureError:
        logger.warning("[AgentIdentity] Token expired for agent %s", _safe_sub(token))
        return None
    except pyjwt.InvalidSignatureError:
        logger.warning("[AgentIdentity] Invalid signature on token")
        return None
    except pyjwt.InvalidTokenError as e:
        logger.warning("[AgentIdentity] Token validation failed: %s", e)
        return None
    except RuntimeError:
        logger.warning("[AgentIdentity] Signing key unavailable for verification")
        return None


def decode_claims(token: str) -> dict | None:
    """Decode JWT claims without full verification (inspection only)."""
    try:
        return pyjwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False},
            algorithms=["HS256"],
            audience=AUDIENCE,
        )
    except pyjwt.InvalidTokenError:
        return None


def hash_token(token: str) -> str:
    """SHA-256 hash of a token string (hex digest).

    Never store raw JWT tokens -- hash only.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def _decode_claims_unverified(token: str) -> AgentIdentityClaims | None:
    """Decode claims without cryptographic verification (kill switch path).

    Signature verification is skipped but expiry IS enforced -- even during
    key rotation emergencies, expired tokens should not be accepted.
    """
    try:
        payload = pyjwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": True},
            algorithms=["HS256"],
            audience=AUDIENCE,
        )
        return _claims_from_payload(payload)
    except pyjwt.ExpiredSignatureError:
        logger.warning("[AgentIdentity] Token expired (kill switch path)")
        return None
    except pyjwt.InvalidTokenError:
        return None


def _claims_from_payload(payload: dict) -> AgentIdentityClaims:
    return AgentIdentityClaims(
        agent_id=payload.get("sub", ""),
        tenant_id=payload.get("tid", ""),
        scopes=payload.get("scopes", []),
        is_meta_agent=payload.get("meta", False),
        issued_at=datetime.fromtimestamp(payload.get("iat", 0), tz=UTC),
        expires_at=datetime.fromtimestamp(payload.get("exp", 0), tz=UTC),
        issuer=payload.get("iss", ""),
    )


def _safe_sub(token: str) -> str:
    """Extract sub claim for logging without full verification."""
    claims = decode_claims(token)
    return claims.get("sub", "unknown") if claims else "unknown"
