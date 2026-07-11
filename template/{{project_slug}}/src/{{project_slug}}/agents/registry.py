"""
AgentRegistry -- Manages local and remote agent registration.

Tracks all agents (in-process Python and remote HTTP), performs health checks,
and provides the agent list to the RoundTable. Persists remote agent
registrations to JSON so they survive restarts.

Usage:
    registry = AgentRegistry(persist_path=Path(".aiscaffold/agents.json"))
    registry.register_local(MyPythonAgent(llm))
    registry.register_remote("ts_analyzer", "code analysis", "http://localhost:3000")
    rt = RoundTable(agents=registry.get_all(), config=config)
"""

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..security import validate_identifier, validate_in_choices
from .capability import AgentCapability
from .identity import hash_token, issue_token, token_expires_at
from .rate_limiter import AgentRateLimiter
from .registry_persistence import (
    VISIBILITY_CHOICES,
    load_remote_entries,
    save_remote_entries,
)
from .remote import RemoteAgent

logger = logging.getLogger(__name__)

DEFAULT_PERSIST_PATH = Path(".aiscaffold/agents.json")

DEFAULT_TENANT = "default"

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

    visibility: "public" (all tenants) / "team" (same tenant) / "private";
    tenant_id: registering tenant (default "default");
    identity_token: raw JWT held in memory only -- NEVER persisted to disk;
    identity_token_hash: SHA-256 of the token (safe to persist);
    identity_expires_at: ISO 8601 UTC expiry of the current token (from
        its exp claim; safe to persist -- it is metadata, not a secret);
    suspended: excluded from dispatch and listings;
    capability: structured AgentCapability (scopes, rate limit);
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
        identity_expires_at: str | None = None,
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
        self.identity_expires_at = identity_expires_at

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
            "expires_at": self.identity_expires_at,
            "last_active": self.last_active,
            "dormant": self.is_dormant,
        }
        if self.agent_type == "remote" and hasattr(self.agent, "_base_url"):
            base["base_url"] = self.agent._base_url
        if hasattr(self.agent, "interaction_count"):
            base["interaction_count"] = self.agent.interaction_count
        return base


class AgentRegistry:
    """
    Manages local and remote agent registration with health checking.

    Remote registrations persist to JSON so they survive process restarts.
    Local agents must be re-registered on startup (they're in-process objects).

    Tenant isolation: entries are keyed by (tenant_id, name), so the same
    name can exist independently in two tenants. Name-based methods take
    an optional ``tenant_id``: a given tenant resolves ONLY within that
    tenant (a miss returns None/False -- callers surface it as 404 so
    tenant existence never leaks); None resolves in "default" first, then
    a unique cross-tenant match -- ambiguous names resolve to nothing
    rather than guessing (dispatch gates use entry_for_agent instead).
    Single-tenant deployments register everything under "default", so
    both paths behave exactly like the historical name-keyed registry.
    """

    def __init__(self, persist_path: Path = DEFAULT_PERSIST_PATH):
        self._agents: dict[tuple[str, str], AgentEntry] = {}
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
    ) -> tuple[str | None, str | None, str | None]:
        """Issue an identity token. Returns (raw_token, token_hash,
        expires_at) where expires_at is the token's exp claim as ISO 8601
        UTC (surfaced so operators can rotate before expiry).

        Failure is non-fatal: local agents are allowed tokenless; remote
        agents are blocked at dispatch until credentials are rotated.
        """
        try:
            scopes = list(capability.access_scopes) if capability else []
            is_meta = capability.is_meta_agent if capability else False
            token, _ttl = issue_token(name, tenant_id, scopes, is_meta_agent=is_meta)
            return token, hash_token(token), token_expires_at(token) or None
        except Exception as e:
            logger.warning(f"[AgentRegistry] Token issuance failed for '{name}': {e}")
            return None, None, None

    def _load_remote_agents(self) -> None:
        """Load persisted remote registrations (see registry_persistence)."""
        for kwargs in load_remote_entries(self._persist_path):
            key = (kwargs.get("tenant_id", DEFAULT_TENANT), kwargs["agent"].name)
            self._agents[key] = AgentEntry(**kwargs)

    def _find_key(
        self, name: str, tenant_id: str | None = None
    ) -> tuple[str, str] | None:
        """Resolve a (tenant_id, name) registry key (see class docstring).
        Entries whose tenant_id attribute was mutated in place after
        registration (a documented platform customization) match by
        attribute when the direct key misses."""
        if tenant_id is not None:
            key = (tenant_id, name)
            if key in self._agents:
                return key
            for k, entry in self._agents.items():
                if k[1] == name and entry.tenant_id == tenant_id:
                    return k
            return None
        default_key = (DEFAULT_TENANT, name)
        if default_key in self._agents:
            return default_key
        matches = [k for k in self._agents if k[1] == name]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                f"[AgentRegistry] Agent name '{name}' exists in multiple "
                "tenants; tenant-less lookup is ambiguous and resolves to none"
            )
        return None

    def _find_entry(
        self, name: str, tenant_id: str | None = None
    ) -> AgentEntry | None:
        key = self._find_key(name, tenant_id)
        return self._agents[key] if key is not None else None

    def _save_remote_agents(self) -> None:
        """Persist remote registrations (raw identity token NEVER written)."""
        save_remote_entries(self._persist_path, list(self._agents.values()))

    def register_local(
        self,
        agent: Any,
        capabilities: list[str] | None = None,
        capability: AgentCapability | None = None,
        tenant_id: str = DEFAULT_TENANT,
    ) -> None:
        """Register an in-process Python agent and issue an identity token.

        tenant_id scopes the registry key -- platform deployments that run
        per-team local agents can register the same name once per tenant.
        """
        if not hasattr(agent, "name") or not hasattr(agent, "domain"):
            raise ValueError("Agent must have 'name' and 'domain' properties")
        tenant_id = validate_identifier(tenant_id, "tenant_id")
        name = agent.name
        if (tenant_id, name) in self._agents:
            logger.warning(f"[AgentRegistry] Replacing existing agent '{name}'")
        token, token_hash, expires_at = self._issue_entry_token(
            name, capability, tenant_id
        )
        self._agents[(tenant_id, name)] = AgentEntry(
            agent=agent,
            agent_type="local",
            capabilities=capabilities,
            tenant_id=tenant_id,
            identity_token=token,
            identity_token_hash=token_hash,
            capability=capability,
            last_active=_now_iso(),
            identity_expires_at=expires_at,
        )
        logger.info(f"[AgentRegistry] Registered local agent: {name}")

    def register_remote(
        self,
        name: str,
        domain: str,
        base_url: str,
        api_key: str = "",
        capabilities: list[str] | None = None,
        timeout: float = 120,
        access_scopes: list[str] | None = None,
        max_calls_per_hour: int | None = None,
        is_meta_agent: bool = False,
        visibility: str = "public",
        tenant_id: str = "default",
    ) -> RemoteAgent:
        """Register a remote agent, issue an identity token, and persist.

        Optional access_scopes / max_calls_per_hour / is_meta_agent build an
        AgentCapability used for scope filtering and rate limiting at
        dispatch. visibility and tenant_id control which tenants can see and
        dispatch this agent; both survive restarts. The raw identity token
        stays in memory only (hash persisted). Raises ValidationError on
        invalid visibility or tenant_id.
        """
        visibility = validate_in_choices(visibility, VISIBILITY_CHOICES, "visibility")
        tenant_id = validate_identifier(tenant_id, "tenant_id")
        agent = RemoteAgent(
            name=name,
            domain=domain,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
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
        if (tenant_id, name) in self._agents:
            logger.warning(
                f"[AgentRegistry] Replacing existing agent '{name}' "
                f"(tenant '{tenant_id}')"
            )
        token, token_hash, expires_at = self._issue_entry_token(
            name, capability, tenant_id
        )
        self._agents[(tenant_id, name)] = AgentEntry(
            agent=agent,
            agent_type="remote",
            capabilities=capabilities,
            visibility=visibility,
            tenant_id=tenant_id,
            identity_token=token,
            identity_token_hash=token_hash,
            capability=capability,
            last_active=_now_iso(),
            identity_expires_at=expires_at,
        )
        self._save_remote_agents()
        logger.info(f"[AgentRegistry] Registered remote agent: {name} at {base_url}")
        return agent

    def unregister(self, name: str, tenant_id: str | None = None) -> bool:
        """Remove an agent from the registry (tenant-scoped when given)."""
        key = self._find_key(name, tenant_id)
        if key is None:
            return False
        agent_type = self._agents[key].agent_type
        del self._agents[key]
        if agent_type == "remote":
            self._save_remote_agents()
        logger.info(f"[AgentRegistry] Unregistered agent: {name}")
        return True

    def _set_suspended(
        self, name: str, suspended: bool, tenant_id: str | None = None
    ) -> bool:
        entry = self._find_entry(name, tenant_id)
        if entry is None:
            return False
        entry.suspended = suspended
        if entry.agent_type == "remote":
            self._save_remote_agents()
        state = "SUSPENDED" if suspended else "UNSUSPENDED"
        logger.warning(f"[AgentRegistry] Agent '{name}' {state}")
        return True

    def suspend(self, name: str, tenant_id: str | None = None) -> bool:
        """Suspend an agent (excluded from dispatch and tenant listings)."""
        return self._set_suspended(name, True, tenant_id)

    def unsuspend(self, name: str, tenant_id: str | None = None) -> bool:
        """Lift an agent's suspension."""
        return self._set_suspended(name, False, tenant_id)

    def rotate_credentials(self, name: str, tenant_id: str | None = None) -> str | None:
        """Re-issue an agent's identity token. Returns the new raw token
        exactly once; None if unknown (in the tenant) or issuance failed."""
        entry = self._find_entry(name, tenant_id)
        if entry is None:
            return None
        token, token_hash, expires_at = self._issue_entry_token(
            name, entry.capability, tenant_id=entry.tenant_id
        )
        if token is None:
            return None
        entry.identity_token = token
        entry.identity_token_hash = token_hash
        entry.identity_expires_at = expires_at
        if entry.agent_type == "remote":
            self._save_remote_agents()
        logger.info(f"[AgentRegistry] Rotated credentials for '{name}'")
        return token

    def revoke_credentials(self, name: str, tenant_id: str | None = None) -> bool:
        """Clear token and hash. Remote agents are blocked at dispatch until
        rotated; local agents fall back to tokenless (allowed) operation."""
        entry = self._find_entry(name, tenant_id)
        if entry is None:
            return False
        entry.identity_token = None
        entry.identity_token_hash = None
        entry.identity_expires_at = None
        if entry.agent_type == "remote":
            self._save_remote_agents()
        logger.warning(f"[AgentRegistry] Revoked credentials for '{name}'")
        return True

    def touch_last_active(self, name: str, tenant_id: str | None = None) -> None:
        """Record dispatch activity (drives dormancy). In-memory only."""
        entry = self._find_entry(name, tenant_id)
        if entry is not None:
            entry.last_active = _now_iso()

    def get(self, name: str, tenant_id: str | None = None) -> Any | None:
        """Get an agent by name (tenant-scoped when tenant_id is given)."""
        entry = self._find_entry(name, tenant_id)
        return entry.agent if entry else None

    def get_entry(self, name: str, tenant_id: str | None = None) -> AgentEntry | None:
        """Get full registry entry (agent + metadata) by name."""
        return self._find_entry(name, tenant_id)

    def entry_for_agent(self, agent: Any) -> AgentEntry | None:
        """Resolve the entry holding this exact agent OBJECT, so dispatch
        gates bind to the entry the orchestrator pulled from the registry
        even when the same name exists in other tenants. Falls back to the
        tenant-less name lookup for agents constructed outside the registry
        (ambiguous names resolve to None; see name_registered)."""
        for entry in self._agents.values():
            if entry.agent is agent:
                return entry
        name = getattr(agent, "name", "")
        return self._find_entry(name) if name else None

    def name_registered(self, name: str) -> bool:
        """True when ANY tenant has this name (dispatch gates fail closed
        on registered-but-unresolvable agents)."""
        return any(k[1] == name for k in self._agents)

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

    async def health_check_all(self, tenant_id: str | None = None) -> dict[str, bool]:
        """Run health checks on remote agents. Returns {name: healthy}.
        tenant_id restricts the pass (and the returned names) to that
        tenant's agents; None checks every registered agent."""
        results = {}
        for (_, name), entry in self._agents.items():
            if tenant_id is not None and entry.tenant_id != tenant_id:
                continue
            if entry.agent_type == "remote" and hasattr(entry.agent, "health_check"):
                healthy = await entry.agent.health_check()
                entry.healthy = healthy
                results[name] = healthy
            else:
                results[name] = True
        return results

    def list_info(self, tenant_id: str | None = None) -> list[dict]:
        """Get serializable info for agents (for API responses).
        tenant_id limits the listing to entries REGISTERED BY that tenant
        (the management view -- suspended agents included, other tenants'
        public agents excluded); None lists everything."""
        return [
            entry.to_dict()
            for entry in self._agents.values()
            if tenant_id is None or entry.tenant_id == tenant_id
        ]

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
