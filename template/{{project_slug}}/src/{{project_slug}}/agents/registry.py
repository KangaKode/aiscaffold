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
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..security import ValidationError, validate_identifier, validate_url
from .remote import RemoteAgent

logger = logging.getLogger(__name__)

DEFAULT_PERSIST_PATH = Path(".aiscaffold/agents.json")


@contextmanager
def _exclusive_file_lock(lock_path: Path):
    """Serialize registry writers that share the same filesystem."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        try:
            import fcntl
        except ImportError:
            yield
            return

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
        self._persist_mtime_ns: int | None = None
        self._load_remote_agents()

    def _persist_mtime(self) -> int | None:
        try:
            return self._persist_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    @staticmethod
    def _default_api_key_env(name: str) -> str:
        return f"AGENT_{name.upper().replace('-', '_')}_API_KEY"

    def _sanitize_api_key_env(self, value: Any, name: str) -> str:
        if isinstance(value, str):
            is_safe = (
                value.startswith("AGENT_")
                and value.endswith("_API_KEY")
                and all(ch == "_" or ch.isdigit() or "A" <= ch <= "Z" for ch in value)
            )
            if is_safe:
                return value
        return self._default_api_key_env(name)

    @staticmethod
    def _sanitize_timeout(value: Any) -> float:
        if isinstance(value, bool):
            return 120
        if isinstance(value, (int, float)) and value > 0:
            return value
        return 120

    @staticmethod
    def _sanitize_mode(value: Any) -> str:
        return value if isinstance(value, str) and value in {"sync", "async"} else "sync"

    @staticmethod
    def _sanitize_capabilities(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)][:50]

    def _read_persisted_remote_agents(self) -> dict[str, dict[str, Any]]:
        """Read and validate persisted remote agent records from disk."""
        if not self._persist_path.exists():
            return {}

        with open(self._persist_path) as f:
            data = json.load(f)

        remote_agents = data.get("remote_agents", [])
        if not isinstance(remote_agents, list):
            raise ValueError("remote_agents must be a list")

        records: dict[str, dict[str, Any]] = {}
        for entry in remote_agents:
            try:
                if not isinstance(entry, dict):
                    raise ValidationError("persisted agent entry must be an object")
                name_value = entry.get("name")
                domain = entry.get("domain")
                base_url_value = entry.get("base_url")
                if not isinstance(name_value, str):
                    raise ValidationError("agent name must be a string")
                if not isinstance(domain, str):
                    raise ValidationError("agent domain must be a string")
                if not isinstance(base_url_value, str):
                    raise ValidationError("base_url must be a string")

                name = validate_identifier(name_value, "agent name")
                base_url = validate_url(base_url_value, "base_url")

                record = {
                    "name": name,
                    "domain": domain,
                    "base_url": base_url,
                    "api_key_env": self._sanitize_api_key_env(
                        entry.get("api_key_env"), name
                    ),
                    "timeout": self._sanitize_timeout(entry.get("timeout", 120)),
                    "mode": self._sanitize_mode(entry.get("mode", "sync")),
                    "agent_type": "remote",
                    "capabilities": self._sanitize_capabilities(
                        entry.get("capabilities", [])
                    ),
                }
                records[name] = record
            except ValidationError as e:
                logger.warning(
                    f"[AgentRegistry] Skipping invalid persisted agent: {e}"
                )
        return records

    def _load_remote_agents(self, replace_existing: bool = False) -> None:
        """Load persisted remote agent registrations from disk.

        API keys are loaded from environment variables (AGENT_{NAME}_API_KEY),
        never from the JSON file. Only the env var name is persisted.
        """
        if not self._persist_path.exists():
            self._persist_mtime_ns = None
            return
        try:
            records = self._read_persisted_remote_agents()
            loaded_agents: dict[str, AgentEntry] = {}
            loaded_count = 0
            for name, entry in records.items():
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
                loaded_agents[name] = AgentEntry(
                    agent=agent,
                    agent_type="remote",
                    capabilities=entry.get("capabilities", []),
                )
                loaded_count += 1
            if replace_existing:
                self._agents = {
                    name: entry
                    for name, entry in self._agents.items()
                    if entry.agent_type != "remote"
                }
            self._agents.update(loaded_agents)
            self._persist_mtime_ns = self._persist_mtime()
            logger.info(
                f"[AgentRegistry] Loaded {loaded_count} "
                f"remote agents from {self._persist_path}"
            )
        except Exception as e:
            logger.warning(f"[AgentRegistry] Failed to load agents: {e}")

    def _refresh_remote_agents_from_disk(self) -> None:
        """Pick up remote agent changes written by another process."""
        current_mtime = self._persist_mtime()
        if current_mtime is None or current_mtime == self._persist_mtime_ns:
            return
        self._load_remote_agents(replace_existing=True)

    def _remote_agent_records(
        self, names: set[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        records = {}
        for name, entry in self._agents.items():
            if names is not None and name not in names:
                continue
            if entry.agent_type == "remote" and hasattr(entry.agent, "to_dict"):
                agent_data = entry.agent.to_dict()
                agent_data["capabilities"] = entry.capabilities
                persisted_name = validate_identifier(agent_data["name"], "agent name")
                base_url = validate_url(agent_data["base_url"], "base_url")
                records[persisted_name] = {
                    "name": persisted_name,
                    "domain": agent_data["domain"],
                    "base_url": base_url,
                    "api_key_env": self._sanitize_api_key_env(
                        agent_data.get("api_key_env"), persisted_name
                    ),
                    "timeout": self._sanitize_timeout(agent_data.get("timeout", 120)),
                    "mode": self._sanitize_mode(agent_data.get("mode", "sync")),
                    "agent_type": "remote",
                    "capabilities": self._sanitize_capabilities(
                        agent_data.get("capabilities", [])
                    ),
                }
        return records

    def _write_remote_agent_records(
        self, records: dict[str, dict[str, Any]]
    ) -> None:
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{self._persist_path.name}.",
            suffix=".tmp",
            dir=self._persist_path.parent,
            text=True,
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as f:
                ordered = [records[name] for name in sorted(records)]
                json.dump({"remote_agents": ordered}, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._persist_path)
            self._persist_mtime_ns = self._persist_mtime()
        except Exception:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            raise

    def _save_remote_agents(
        self,
        upsert_names: set[str] | None = None,
        remove_names: set[str] | None = None,
    ) -> None:
        """Persist remote agent registrations to disk."""
        removals = remove_names or set()
        upsert_filter = upsert_names
        if upsert_filter is None and removals:
            upsert_filter = set()
        upserts = self._remote_agent_records(upsert_filter)

        lock_path = self._persist_path.with_suffix(self._persist_path.suffix + ".lock")
        with _exclusive_file_lock(lock_path):
            try:
                persisted = self._read_persisted_remote_agents()
            except Exception as e:
                logger.warning(
                    f"[AgentRegistry] Rebuilding persisted agents after read failure: {e}"
                )
                persisted = {}

            for name in removals:
                persisted.pop(name, None)
            persisted.update(upserts)
            self._write_remote_agent_records(persisted)
        self._load_remote_agents(replace_existing=True)
        logger.debug(
            f"[AgentRegistry] Saved {len(upserts)} remote agent updates"
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
        name = validate_identifier(name, "agent name")
        if not isinstance(domain, str):
            raise ValidationError("agent domain must be a string")
        base_url = validate_url(base_url, "base_url")
        capabilities = self._sanitize_capabilities(capabilities or [])
        mode = self._sanitize_mode(mode)
        timeout = self._sanitize_timeout(timeout)
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
        self._save_remote_agents(upsert_names={name})
        logger.info(f"[AgentRegistry] Registered remote agent: {name} at {base_url}")
        return agent

    def unregister(self, name: str) -> bool:
        """Remove an agent from the registry."""
        self._refresh_remote_agents_from_disk()
        if name not in self._agents:
            return False
        agent_type = self._agents[name].agent_type
        del self._agents[name]
        if agent_type == "remote":
            self._save_remote_agents(remove_names={name})
        logger.info(f"[AgentRegistry] Unregistered agent: {name}")
        return True

    def get(self, name: str) -> Any | None:
        """Get an agent by name."""
        self._refresh_remote_agents_from_disk()
        entry = self._agents.get(name)
        return entry.agent if entry else None

    def get_entry(self, name: str) -> AgentEntry | None:
        """Get full registry entry (agent + metadata) by name."""
        self._refresh_remote_agents_from_disk()
        return self._agents.get(name)

    def get_all(self) -> list:
        """Get all registered agents (for passing to RoundTable)."""
        self._refresh_remote_agents_from_disk()
        return [entry.agent for entry in self._agents.values()]

    def get_all_entries(self) -> list[AgentEntry]:
        """Get all registry entries with metadata."""
        self._refresh_remote_agents_from_disk()
        return list(self._agents.values())

    def get_by_capability(self, capability: str) -> list:
        """Get agents that have a specific capability tag."""
        self._refresh_remote_agents_from_disk()
        return [
            entry.agent
            for entry in self._agents.values()
            if capability in entry.capabilities
        ]

    async def health_check_all(self) -> dict[str, bool]:
        """Run health checks on all remote agents. Returns {name: healthy}."""
        self._refresh_remote_agents_from_disk()
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
        self._refresh_remote_agents_from_disk()
        return [entry.to_dict() for entry in self._agents.values()]

    def list_for_tenant(self, tenant_id: str = "default") -> list[AgentEntry]:
        """Get agents visible to a specific tenant.

        Visibility rules:
          - "public": visible to all tenants
          - "team": visible only to the registering tenant
          - "private": visible only to the registering user (not filtered here)
        """
        self._refresh_remote_agents_from_disk()
        return [
            entry for entry in self._agents.values()
            if entry.visibility == "public" or entry.tenant_id == tenant_id
        ]

    @property
    def count(self) -> int:
        """Total number of registered agents."""
        self._refresh_remote_agents_from_disk()
        return len(self._agents)

    @property
    def remote_count(self) -> int:
        """Number of remote agents."""
        self._refresh_remote_agents_from_disk()
        return sum(1 for e in self._agents.values() if e.agent_type == "remote")

    @property
    def local_count(self) -> int:
        """Number of local agents."""
        self._refresh_remote_agents_from_disk()
        return sum(1 for e in self._agents.values() if e.agent_type == "local")
