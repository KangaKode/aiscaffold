"""Shared agent identity verification for orchestration layer.

Single policy point used by orchestrators before dispatching to an agent,
so RoundTable and ChatOrchestrator enforce identical rules.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def verify_agent_identity(
    agent: Any,
    registry: Any | None,
    caller: str = "Orchestrator",
) -> bool:
    """Verify agent identity token before dispatch.

    Returns True if the agent is allowed to participate.

    Policy:
    - No registry, or agent not in registry: allowed (backward compatible).
    - Suspended agents: blocked.
    - Local agents without tokens: allowed (backward compatible).
    - Remote agents without tokens: blocked.
    - Invalid/expired tokens: blocked.
    """
    if registry is None:
        return True

    entry = registry.get_entry(agent.name)
    if entry is None:
        return True

    if getattr(entry, "suspended", False):
        logger.warning(
            "[%s] Agent '%s' is suspended — skipping",
            caller,
            agent.name,
        )
        return False

    identity_token = getattr(entry, "identity_token", None)
    if identity_token is None:
        if entry.agent_type == "remote":
            logger.warning(
                "[%s] Remote agent '%s' has no identity token — skipping",
                caller,
                agent.name,
            )
            return False
        return True

    from ..agents.identity import verify_token

    claims = verify_token(identity_token)
    if claims is None:
        logger.warning(
            "[%s] Agent '%s' identity verification failed — skipping",
            caller,
            agent.name,
        )
        return False
    return True
