"""
Opt-in runtime canary inject + observe for final caller-visible LLM surfaces.

Both toggles default OFF. Detection mutates prompts only when enabled
(never silently always-on). Enforcement refuses only when detection is
also on. Security primitives stay in security/; this module owns wiring
helpers and fire-and-forget flag persistence.

Surfaces (v1): chat_synthesis, resolve, round_table_synthesis.

Keep this file under 200 lines.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from ..learning.flags import insert_flag_once
from ..security.injection_defense import inject_canary
from ..security.prompt_guard import wrap_user_content

logger = logging.getLogger(__name__)

DETECTION_ENV = "RUNTIME_CANARY_ENABLED"
ENFORCEMENT_ENV = "RUNTIME_CANARY_ENFORCEMENT_ENABLED"
FLAG_TYPE = "canary_leak"
REFUSAL_SOURCE = "canary"
REFUSAL_REASON = "canary_leak"

SURFACE_CHAT = "chat_synthesis"
SURFACE_RESOLVE = "resolve"
SURFACE_ROUND_TABLE = "round_table_synthesis"


def _truthy(var: str) -> bool:
    return os.environ.get(var, "").strip().lower() in ("true", "1", "yes")


def detection_enabled() -> bool:
    """True when RUNTIME_CANARY_ENABLED is truthy."""
    return _truthy(DETECTION_ENV)


def enforcement_enabled() -> bool:
    """Refuse mode — no-op unless detection is also on."""
    return detection_enabled() and _truthy(ENFORCEMENT_ENV)


def wrap_chat_user(
    content: str, label: str = "USER_CONTENT"
) -> tuple[str, str | None]:
    """Chat synthesis user field: unwrapped when off; wrap+canary when on."""
    if not detection_enabled():
        return content, None
    wrapped, token = wrap_user_content(content, label=label, canary=True)
    return wrapped, token


def wrap_resolve_query(
    content: str, label: str = "TASK_CONTENT"
) -> tuple[str, str | None]:
    """Resolve query: always XML-wrapped; canary only when detection on."""
    if not detection_enabled():
        return wrap_user_content(content, label=label), None
    wrapped, token = wrap_user_content(content, label=label, canary=True)
    return wrapped, token


def canary_context_section(
    content: str, label: str = "SYNTHESIS_ANALYSES"
) -> tuple[str, str | None]:
    """Round-table untrusted analyses blob; trusted instruction untouched."""
    if not detection_enabled():
        return content, None
    return inject_canary(content, label)


def observe_response(
    text: str,
    token: str | None,
    *,
    store,
    tenant_id: str,
    surface: str,
) -> bool:
    """Sync check + flag. Callers should run via asyncio.to_thread."""
    if not token:
        return False
    from ..security import check_canary

    leaked = check_canary(text or "", token)
    if not leaked:
        return False
    if store is not None:
        try:
            insert_flag_once(
                store,
                FLAG_TYPE,
                subject_id=surface,
                tenant_id=tenant_id or "default",
                detail={"surface": surface},
                severity="warning",
            )
        except Exception as exc:
            logger.warning(
                "[RuntimeCanary] Flag persist failed (ignored): %s",
                type(exc).__name__,
            )
    return True


def should_refuse(leaked: bool) -> bool:
    """True when a leak was observed and enforcement is enabled."""
    return bool(leaked) and enforcement_enabled()


@dataclass(frozen=True)
class CanaryRefusal:
    """Bounded refuse contract for round-table (parallel to SentinelRefusal)."""

    reason: str = REFUSAL_REASON
