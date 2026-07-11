"""Shared agent identity verification for orchestration layer.

Single policy point used by orchestrators before dispatching to an agent,
so RoundTable and ChatOrchestrator enforce identical rules.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Block reasons exposed via verify_agent_identity's reason_out parameter.
# (Names put TOKEN first so secret scanners don't mistake the reason
# strings for credential literals.)
REASON_AMBIGUOUS_NAME = "ambiguous_name"
REASON_SUSPENDED = "suspended"
REASON_TOKEN_MISSING = "missing_token"
REASON_TOKEN_INVALID_OR_EXPIRED = "invalid_or_expired_token"


def resolve_registry_entry(registry: Any, agent: Any) -> Any | None:
    """Resolve the registry entry for an agent by OBJECT identity.

    Prefers registry.entry_for_agent (binds to the exact entry the
    orchestrator pulled from the registry, immune to same-name entries
    in other tenants); falls back to the historical name lookup for
    registry implementations without it.
    """
    resolver = getattr(registry, "entry_for_agent", None)
    if resolver is not None:
        return resolver(agent)
    return registry.get_entry(agent.name)


def verify_agent_identity(
    agent: Any,
    registry: Any | None,
    caller: str = "Orchestrator",
    reason_out: list | None = None,
) -> bool:
    """Verify agent identity token before dispatch.

    Returns True if the agent is allowed to participate. When blocked
    and reason_out (a list) is given, one REASON_* string is appended
    so callers can distinguish an administrative suspension from a
    credential problem -- the bool contract is unchanged for callers
    that ignore it.

    Policy:
    - No registry, or agent not in registry: allowed (backward compatible).
    - Registered name that cannot be resolved to one entry (same name in
      multiple tenants, agent object unknown to the registry): blocked
      (fail closed -- never treat a registered-but-ambiguous agent as
      unregistered).
    - Suspended agents: blocked.
    - Local agents without tokens: allowed (backward compatible).
    - Remote agents without tokens: blocked.
    - Invalid/expired tokens: blocked.
    """

    def _blocked(reason: str) -> bool:
        if reason_out is not None:
            reason_out.append(reason)
        return False

    if registry is None:
        return True

    entry = resolve_registry_entry(registry, agent)
    if entry is None:
        name_registered = getattr(registry, "name_registered", None)
        if name_registered is not None and name_registered(agent.name):
            logger.warning(
                "[%s] Agent '%s' is registered but cannot be resolved to a "
                "single registry entry (same name in multiple tenants?) — "
                "blocking dispatch (fail closed)",
                caller,
                agent.name,
            )
            return _blocked(REASON_AMBIGUOUS_NAME)
        return True

    if getattr(entry, "suspended", False):
        logger.warning(
            "[%s] Agent '%s' is suspended — skipping",
            caller,
            agent.name,
        )
        return _blocked(REASON_SUSPENDED)

    identity_token = getattr(entry, "identity_token", None)
    if identity_token is None:
        if entry.agent_type == "remote":
            logger.warning(
                "[%s] Remote agent '%s' has no identity token — skipping",
                caller,
                agent.name,
            )
            return _blocked(REASON_TOKEN_MISSING)
        return True

    from ..agents.identity import verify_token

    claims = verify_token(identity_token)
    if claims is None:
        logger.warning(
            "[%s] Agent '%s' identity verification failed — skipping",
            caller,
            agent.name,
        )
        return _blocked(REASON_TOKEN_INVALID_OR_EXPIRED)
    return True
