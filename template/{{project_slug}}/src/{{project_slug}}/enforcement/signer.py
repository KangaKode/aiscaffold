"""
Output signer -- platform attestation for synthesized outputs.

Signs a deliberation's final output with an HMAC-SHA256 tag so a consumer
can verify it was produced by this platform and has not been altered in
transit or storage. Pairs with the reasoning chain hash: the chain hash
proves the *process* record is intact, the signature proves the *output*
is authentic.

Key handling mirrors agents/identity.py: OUTPUT_SIGNING_KEY from the
environment (>= 64 chars, required in production), an ephemeral
auto-generated key in development so tests and local runs just work.

Scope, honestly: this is symmetric (HMAC), so anyone who can verify can
also sign. It gives integrity and platform attestation for consumers that
share the key (your own services), not third-party non-repudiation. For
that, swap the HMAC for an asymmetric signature (Ed25519) with the same
interface -- see GOVERNANCE.md.

Zero third-party dependencies -- stdlib only.
"""

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SIGNATURE_VERSION = 1
_MIN_KEY_CHARS = 64
_signing_key_cache: str | None = None


@dataclass(frozen=True)
class SignedOutput:
    """An output plus its detached attestation."""

    payload: str
    signature: str
    version: int


def _is_production() -> bool:
    """Same convention as api.middleware.auth / agents.identity."""
    env = os.environ.get("ENV", os.environ.get("ENVIRONMENT", "development"))
    return env.lower() in ("production", "prod", "staging")


def _get_signing_key() -> str:
    """Resolve the output signing key (env, else ephemeral dev key).

    Cached per process. In production a real key is required; a too-short
    or missing key raises rather than silently signing with a weak key.
    """
    global _signing_key_cache
    if _signing_key_cache is not None:
        return _signing_key_cache

    key = os.environ.get("OUTPUT_SIGNING_KEY", "").strip()
    if key:
        if len(key) < _MIN_KEY_CHARS:
            if _is_production():
                raise RuntimeError(
                    f"OUTPUT_SIGNING_KEY is too short (got {len(key)} chars, "
                    f"minimum {_MIN_KEY_CHARS} required). Generate one: "
                    'python -c "import secrets; print(secrets.token_hex(32))"'
                )
            logger.warning(
                "[OutputSigner] Signing key < %d chars; using ephemeral key",
                _MIN_KEY_CHARS,
            )
            key = secrets.token_hex(32)
        _signing_key_cache = key
        return key

    if _is_production():
        raise RuntimeError(
            "OUTPUT_SIGNING_KEY is required in production. Generate one: "
            'python -c "import secrets; print(secrets.token_hex(32))"'
        )
    logger.info("[OutputSigner] No OUTPUT_SIGNING_KEY set; using ephemeral dev key")
    _signing_key_cache = secrets.token_hex(32)
    return _signing_key_cache


def _compute(payload: str, key: str) -> str:
    return hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_output(payload: str) -> SignedOutput:
    """Attest an output. Returns the payload plus its HMAC signature."""
    signature = _compute(payload, _get_signing_key())
    return SignedOutput(payload=payload, signature=signature, version=SIGNATURE_VERSION)


def verify_output(payload: str, signature: str) -> bool:
    """Constant-time check that signature matches payload under the key."""
    expected = _compute(payload, _get_signing_key())
    return hmac.compare_digest(expected, signature)


def reset_signing_key_cache() -> None:
    """Clear the cached key (test helper; call after changing the env)."""
    global _signing_key_cache
    _signing_key_cache = None
