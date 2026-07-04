"""
AgentRegistry -- Manages local and remote agent registration.

Tracks all agents (in-process Python and remote HTTP), performs health checks,
and provides the agent list to the RoundTable. Persists remote agent
registrations to JSON so they survive restarts.

Usage:
    registry = AgentRegistry(persist_path=Path(".aiscaffold/agents.json"))

    # Local agents
    registry.register_local(MyPythonAgent(llm))

    # Remote agents (any language)
    registry.register_remote("ts_analyzer", "code analysis", "http://localhost:3000")

    # Pass all agents to round table
    rt = RoundTable(agents=registry.get_all(), config=config)
"""

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..security import validate_identifier, validate_in_choices
from .capability import AgentCapability
from .identity import hash_token, issue_token
from .rate_limiter import AgentRateLimiter
from .registry_persistence import (
    VISIBILITY_CHOICES,
    load_remote_entries,
    save_remote_entries,
)
from .remote import RemoteAgent

logger = logging.getLogger(__name__)

DEFAULT_PERSIST_PATH = Path(".aiscaffold/agents.json")

DORMANT_AFTER_DAYS = 30


def _dormant_after_days() -> int:
    """Dormancy threshold in days (env AGENT_DORMANT_AFTER_DAYS overrides)."""
    raw = os.environ.get("AGENT_DORMANT_AFTER_DAYS")
    if raw is not None:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
        logger.warning(
            "[AgentRegistry] Invalid AGENT_DORMANT_AFTER_DAYS, using default %d",
            DORMANT_AFTER_DAYS,
        )
    return DORMANT_AFTER_DAYS


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@runtime_checkable
class AgentLike(Protocol):
    """Minimal interface for agent identity."""

    @property
    def name(self) -> str: ...

    @property
    def domain(self) -> str: ...


class AgentEntry:
    """Internal registry entry wrapping an agent with metadata.

    Multi-tenancy fields:
        visibility: "public" (all tenants), "team" (same tenant), "private" (registering user)
        tenant_id: The tenant that registered this agent. Defaults to "default".

    Identity fields:
        identity_token: Raw JWT held in memory only -- NEVER persisted to disk.
        identity_token_hash: SHA-256 hash of the token (safe to persist).
        suspended: Suspended agents are excluded from dispatch and listings.
        capability: Structured AgentCapability (scopes, rate limit).
        last_active: ISO timestamp of last dispatch (drives dormancy).
    """

    def __init__(
        self,
        agent: Any,
        agent_type: str = "local",
        capabilities: list[str] | None = None,
        visibility: str = "public",
        tenant_id: str = "default",
        identity_token: str | None = None,
        identity_token_hash: str | None = None,
        suspended: bool = False,
        capability: AgentCapability | None = None,
        last_active: str | None = None,
    ):
        self.agent = agent
        self.agent_type = agent_type
        self.capabilities = capabilities or []
        self.healthy = True
        self.visibility = visibility
        self.tenant_id = tenant_id
        self.identity_token = identity_token
        self.identity_token_hash = identity_token_hash
        self.suspended = suspended
        self.capability = capability
        self.last_active = last_active

    @property
    def is_dormant(self) -> bool:
        """True if the agent's last activity is older than the dormancy threshold."""
        if not self.last_active:
            return False
        try:
            last = datetime.fromisoformat(self.last_active)
        except ValueError:
            return False
        age_days = (datetime.now(UTC) - last).total_seconds() / 86400
        return age_days > _dormant_after_days()

    def to_dict(self) -> dict:
        """Serialize for API responses."""
        base = {
            "name": self.agent.name,
            "domain": self.agent.domain,
            "agent_type": self.agent_type,
            "capabilities": self.capabilities,
            "healthy": self.healthy,
            "visibility": self.visibility,
            "tenant_id": self.tenant_id,
            "suspended": self.suspended,
            "credential_status": "active" if self.identity_token_hash else "none",
            "last_active": self.last_active,
            "dormant": self.is_dormant,
        }
        if self.agent_type == "remote" and hasattr(self.agent, "_base_url"):
            base["base_url"] = self.agent._base_url
            base["mode"] = getattr(self.agent, "_mode", "sync")
        if hasattr(self.agent, "interaction_count"):
            base["interaction_count"] = self.agent.interaction_count
        return base


class AgentRegistry:
    """
    Manages local and remote agent registration with health checking.

    Remote registrations persist to JSON so they survive process restarts.
    Local agents must be re-registered on startup (they're in-process objects).
    """

    def __init__(self, persist_path: Path = DEFAULT_PERSIST_PATH):
        self._agents: dict[str, AgentEntry] = {}
        self._persist_path = persist_path
        self._rate_limiter: AgentRateLimiter | None = None
        self._load_remote_agents()

    @property
    def rate_limiter(self) -> AgentRateLimiter:
        """Shared per-agent rate limiter (lazily constructed)."""
        if self._rate_limiter is None:
            self._rate_limiter = AgentRateLimiter()
        return self._rate_limiter

    def _issue_entry_token(
        self,
        name: str,
        capability: AgentCapability | None,
        tenant_id: str = "default",
    ) -> tuple[str | None, str | None]:
        """Issue an identity token. Returns (raw_token, token_hash).

        Failure is non-fatal: local agents are allowed tokenless; remote
        agents are blocked at dispatch until credentials are rotated.
        """
        try:
            scopes = list(capability.access_scopes) if capability else []
            is_meta = capability.is_meta_agent if capability else False
            token, _ttl = issue_token(name, tenant_id, scopes, is_meta_agent=is_meta)
            return token, hash_token(token)
        except Exception as e:
            logger.warning(f"[AgentRegistry] Token issuance failed for '{name}': {e}")
            return None, None

    def _load_remote_agents(self) -> None:
        """Load persisted remote agent registrations (validation and env-based
        credential resolution live in registry_persistence)."""
        for kwargs in load_remote_entries(self._persist_path):
            self._agents[kwargs["agent"].name] = AgentEntry(**kwargs)

    def _save_remote_agents(self) -> None:
        """Persist remote agent registrations to disk. The raw identity token
        is NEVER written -- only its hash and visibility/tenant metadata."""
        save_remote_entries(self._persist_path, list(self._agents.values()))

    def register_local(
        self,
        agent: Any,
        capabilities: list[str] | None = None,
        capability: AgentCapability | None = None,
    ) -> None:
        """Register an in-process Python agent and issue an identity token."""
        if not hasattr(agent, "name") or not hasattr(agent, "domain"):
            raise ValueError("Agent must have 'name' and 'domain' properties")
        name = agent.name
        if name in self._agents:
            logger.warning(f"[AgentRegistry] Replacing existing agent '{name}'")
        token, token_hash = self._issue_entry_token(name, capability)
        self._agents[name] = AgentEntry(
            agent=agent,
            agent_type="local",
            capabilities=capabilities,
            identity_token=token,
            identity_token_hash=token_hash,
            capability=capability,
            last_active=_now_iso(),
        )
        logger.info(f"[AgentRegistry] Registered local agent: {name}")

    def register_remote(
        self,
        name: str,
        domain: str,
        base_url: str,
        api_key: str = "",
        capabilities: list[str] | None = None,
        mode: str = "sync",
        timeout: float = 120,
        access_scopes: list[str] | None = None,
        max_calls_per_hour: int | None = None,
        is_meta_agent: bool = False,
        visibility: str = "public",
        tenant_id: str = "default",
    ) -> RemoteAgent:
        """Register a remote agent, issue an identity token, and persist.

        Optional access_scopes / max_calls_per_hour / is_meta_agent build an
        AgentCapability used for scope filtering and rate limiting at dispatch.
        visibility ("public"/"team"/"private") and tenant_id control which
        tenants can see and dispatch this agent; both survive restarts.
        The raw identity token is held in memory only (hash persisted).

        Raises ValidationError on invalid visibility or tenant_id.
        """
        visibility = validate_in_choices(visibility, VISIBILITY_CHOICES, "visibility")
        tenant_id = validate_identifier(tenant_id, "tenant_id")
        agent = RemoteAgent(
            name=name,
            domain=domain,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            mode=mode,
        )
        capability = None
        if access_scopes or max_calls_per_hour is not None or is_meta_agent:
            default_max = AgentCapability().max_calls_per_hour
            capability = AgentCapability(
                access_scopes=list(access_scopes or []),
                is_meta_agent=is_meta_agent,
                max_calls_per_hour=(
                    max_calls_per_hour if max_calls_per_hour is not None else default_max
                ),
            )
        token, token_hash = self._issue_entry_token(name, capability, tenant_id)
        self._agents[name] = AgentEntry(
            agent=agent,
            agent_type="remote",
            capabilities=capabilities,
            visibility=visibility,
            tenant_id=tenant_id,
            identity_token=token,
            identity_token_hash=token_hash,
            capability=capability,
            last_active=_now_iso(),
        )
        self._save_remote_agents()
        logger.info(f"[AgentRegistry] Registered remote agent: {name} at {base_url}")
        return agent

    def unregister(self, name: str) -> bool:
        """Remove an agent from the registry."""
        if name not in self._agents:
            return False
        agent_type = self._agents[name].agent_type
        del self._agents[name]
        if agent_type == "remote":
            self._save_remote_agents()
        logger.info(f"[AgentRegistry] Unregistered agent: {name}")
        return True

    def _set_suspended(self, name: str, suspended: bool) -> bool:
        entry = self._agents.get(name)
        if entry is None:
            return False
        entry.suspended = suspended
        if entry.agent_type == "remote":
            self._save_remote_agents()
        state = "SUSPENDED" if suspended else "UNSUSPENDED"
        logger.warning(f"[AgentRegistry] Agent '{name}' {state}")
        return True

    def suspend(self, name: str) -> bool:
        """Suspend an agent (excluded from dispatch and tenant listings)."""
        return self._set_suspended(name, True)

    def unsuspend(self, name: str) -> bool:
        """Lift an agent's suspension."""
        return self._set_suspended(name, False)

    def rotate_credentials(self, name: str) -> str | None:
        """Re-issue an agent's identity token. Returns the new raw token
        exactly once (only the hash is retained on disk); None if the
        agent is unknown or issuance failed."""
        entry = self._agents.get(name)
        if entry is None:
            return None
        token, token_hash = self._issue_entry_token(
            name, entry.capability, tenant_id=entry.tenant_id
        )
        if token is None:
            return None
        entry.identity_token = token
        entry.identity_token_hash = token_hash
        if entry.agent_type == "remote":
            self._save_remote_agents()
        logger.info(f"[AgentRegistry] Rotated credentials for '{name}'")
        return token

    def revoke_credentials(self, name: str) -> bool:
        """Clear an agent's identity token and hash. Remote agents are
        blocked at dispatch until credentials are rotated; local agents
        fall back to tokenless (allowed) operation."""
        entry = self._agents.get(name)
        if entry is None:
            return False
        entry.identity_token = None
        entry.identity_token_hash = None
        if entry.agent_type == "remote":
            self._save_remote_agents()
        logger.warning(f"[AgentRegistry] Revoked credentials for '{name}'")
        return True

    def touch_last_active(self, name: str) -> None:
        """Record dispatch activity (drives dormancy). In-memory only;
        persisted opportunistically on the next remote-registration save."""
        entry = self._agents.get(name)
        if entry is not None:
            entry.last_active = _now_iso()

    def get(self, name: str) -> Any | None:
        """Get an agent by name."""
        entry = self._agents.get(name)
        return entry.agent if entry else None

    def get_entry(self, name: str) -> AgentEntry | None:
        """Get full registry entry (agent + metadata) by name."""
        return self._agents.get(name)

    def get_all(self) -> list:
        """Get all registered agents (for passing to RoundTable)."""
        return [entry.agent for entry in self._agents.values()]

    def get_all_entries(self) -> list[AgentEntry]:
        """Get all registry entries with metadata."""
        return list(self._agents.values())

    def get_by_capability(self, capability: str) -> list:
        """Get agents that have a specific capability tag."""
        return [
            entry.agent
            for entry in self._agents.values()
            if capability in entry.capabilities
        ]

    async def health_check_all(self) -> dict[str, bool]:
        """Run health checks on all remote agents. Returns {name: healthy}."""
        results = {}
        for name, entry in self._agents.items():
            if entry.agent_type == "remote" and hasattr(entry.agent, "health_check"):
                healthy = await entry.agent.health_check()
                entry.healthy = healthy
                results[name] = healthy
            else:
                results[name] = True
        return results

    def list_info(self) -> list[dict]:
        """Get serializable info for all agents (for API responses)."""
        return [entry.to_dict() for entry in self._agents.values()]

    def list_for_tenant(self, tenant_id: str = "default") -> list[AgentEntry]:
        """Get agents visible to a specific tenant (suspended agents excluded).

        Visibility rules:
          - "public": visible to all tenants
          - "team": visible only to the registering tenant
          - "private": visible only to the registering user (not filtered here)
        """
        return [
            entry for entry in self._agents.values()
            if not entry.suspended
            and (entry.visibility == "public" or entry.tenant_id == tenant_id)
        ]

    @property
    def count(self) -> int:
        """Total number of registered agents."""
        return len(self._agents)

    @property
    def remote_count(self) -> int:
        """Number of remote agents."""
        return sum(1 for e in self._agents.values() if e.agent_type == "remote")

    @property
    def local_count(self) -> int:
        """Number of local agents."""
        return sum(1 for e in self._agents.values() if e.agent_type == "local")
