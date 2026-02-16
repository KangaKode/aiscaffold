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

import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .remote import RemoteAgent

logger = logging.getLogger(__name__)

DEFAULT_PERSIST_PATH = Path(".aiscaffold/agents.json")


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
    """

    def __init__(
        self,
        agent: Any,
        agent_type: str = "local",
        capabilities: list[str] | None = None,
        visibility: str = "public",
        tenant_id: str = "default",
    ):
        self.agent = agent
        self.agent_type = agent_type
        self.capabilities = capabilities or []
        self.healthy = True
        self.visibility = visibility
        self.tenant_id = tenant_id

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
        self._load_remote_agents()

    def _load_remote_agents(self) -> None:
        """Load persisted remote agent registrations from disk.

        API keys are loaded from environment variables (AGENT_{NAME}_API_KEY),
        never from the JSON file. Only the env var name is persisted.
        """
        if not self._persist_path.exists():
            return
        try:
            with open(self._persist_path) as f:
                data = json.load(f)
            for entry in data.get("remote_agents", []):
                name = entry["name"]
                api_key_env = entry.get("api_key_env", f"AGENT_{name.upper()}_API_KEY")
                api_key = os.environ.get(api_key_env, "")

                agent = RemoteAgent(
                    name=name,
                    domain=entry["domain"],
                    base_url=entry["base_url"],
                    api_key=api_key,
                    timeout=entry.get("timeout", 120),
                    mode=entry.get("mode", "sync"),
                )
                self._agents[name] = AgentEntry(
                    agent=agent,
                    agent_type="remote",
                    capabilities=entry.get("capabilities", []),
                )
            logger.info(
                f"[AgentRegistry] Loaded {len(data.get('remote_agents', []))} "
                f"remote agents from {self._persist_path}"
            )
        except Exception as e:
            logger.warning(f"[AgentRegistry] Failed to load agents: {e}")

    def _save_remote_agents(self) -> None:
        """Persist remote agent registrations to disk."""
        remote_entries = []
        for entry in self._agents.values():
            if entry.agent_type == "remote" and hasattr(entry.agent, "to_dict"):
                agent_data = entry.agent.to_dict()
                agent_data["capabilities"] = entry.capabilities
                remote_entries.append(agent_data)

        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._persist_path, "w") as f:
            json.dump({"remote_agents": remote_entries}, f, indent=2)
        logger.debug(
            f"[AgentRegistry] Saved {len(remote_entries)} remote agents"
        )

    def register_local(
        self,
        agent: Any,
        capabilities: list[str] | None = None,
    ) -> None:
        """Register an in-process Python agent."""
        if not hasattr(agent, "name") or not hasattr(agent, "domain"):
            raise ValueError("Agent must have 'name' and 'domain' properties")
        name = agent.name
        if name in self._agents:
            logger.warning(f"[AgentRegistry] Replacing existing agent '{name}'")
        self._agents[name] = AgentEntry(
            agent=agent, agent_type="local", capabilities=capabilities
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
    ) -> RemoteAgent:
        """Register a remote agent and persist the registration."""
        agent = RemoteAgent(
            name=name,
            domain=domain,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            mode=mode,
        )
        self._agents[name] = AgentEntry(
            agent=agent, agent_type="remote", capabilities=capabilities
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
        """Get agents visible to a specific tenant.

        Visibility rules:
          - "public": visible to all tenants
          - "team": visible only to the registering tenant
          - "private": visible only to the registering user (not filtered here)
        """
        return [
            entry for entry in self._agents.values()
            if entry.visibility == "public" or entry.tenant_id == tenant_id
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
