"""
MCP server registry -- tracks registered MCP servers per tenant.

In-memory dict with optional JSON file persistence (MCP_REGISTRY_PATH).
Enterprise deployments can swap this for a database-backed registry with
the same four methods.

Security invariants:
  - Credentials are stored as env var REFERENCES (``credential_env_var``,
    must match ^MCP_[A-Z0-9_]+$), never as raw tokens -- the secret is
    resolved from the environment at call time and a persisted registry
    file never contains it.
  - ``scope_key`` must start with "mcp:"; the scope is what gates which
    agents can see the server's data (agents/capability.py access_scopes).
  - Server URLs pass validate_url (anti-SSRF) on register AND on load,
    so a tampered persistence file cannot inject unsafe entries.

Keep this file under 250 lines.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from ..security.validators import (
    ValidationError,
    validate_identifier,
    validate_in_choices,
    validate_url,
)

logger = logging.getLogger(__name__)

_CREDENTIAL_ENV_RE = re.compile(r"^MCP_[A-Z0-9_]+$")
VALID_AUTH_TYPES = ["bearer", "api_key", "none"]
SCOPE_PREFIX = "mcp:"


@dataclass
class MCPServerConfig:
    """Configuration for one MCP server endpoint.

    default_tool / default_arguments describe the enrichment call made
    when a round-table task needs this server's scope (see
    orchestration/mcp_enrichment.py).
    """

    name: str
    server_url: str
    scope_key: str
    tenant_id: str
    auth_type: str = "bearer"
    credential_env_var: str = ""
    enabled: bool = True
    timeout: float = 30.0
    default_tool: str = ""
    default_arguments: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.credential_env_var and not _CREDENTIAL_ENV_RE.match(
            self.credential_env_var
        ):
            raise ValidationError(
                "credential_env_var must match ^MCP_[A-Z0-9_]+$"
                f" (got '{self.credential_env_var}')"
            )

    def resolve_credential(self) -> str | None:
        """Resolve the secret from the environment at call time."""
        if not self.credential_env_var:
            return None
        return os.environ.get(self.credential_env_var)

    def __repr__(self) -> str:
        return (
            f"MCPServerConfig(name={self.name!r}, server_url={self.server_url!r}, "
            f"scope_key={self.scope_key!r}, tenant_id={self.tenant_id!r}, "
            f"auth_type={self.auth_type!r}, credential_env_var=<CONFIGURED>, "
            f"enabled={self.enabled}, timeout={self.timeout})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistence (credential stays an env var name)."""
        return {
            "name": self.name,
            "server_url": self.server_url,
            "scope_key": self.scope_key,
            "tenant_id": self.tenant_id,
            "auth_type": self.auth_type,
            "credential_env_var": self.credential_env_var,
            "enabled": self.enabled,
            "timeout": self.timeout,
            "default_tool": self.default_tool,
            "default_arguments": self.default_arguments,
        }


class MCPServerRegistry:
    """Per-tenant MCP server registry with optional JSON persistence."""

    def __init__(self, persist_path: str | None = None) -> None:
        self._servers: dict[str, dict[str, MCPServerConfig]] = {}
        self._persist_path = persist_path
        if persist_path and os.path.exists(persist_path):
            self.load()

    @staticmethod
    def _validate_config(config: MCPServerConfig) -> None:
        validate_url(config.server_url, field_name="server_url")
        validate_identifier(config.name, field_name="name")
        validate_in_choices(
            config.auth_type, VALID_AUTH_TYPES, field_name="auth_type"
        )
        if not config.scope_key.startswith(SCOPE_PREFIX):
            raise ValidationError(
                f"scope_key must start with '{SCOPE_PREFIX}' (got '{config.scope_key}')"
            )

    def register(self, config: MCPServerConfig) -> None:
        """Register a server for a tenant (validated; duplicate names rejected)."""
        self._validate_config(config)
        tenant_servers = self._servers.setdefault(config.tenant_id, {})
        if config.name in tenant_servers:
            raise ValidationError(
                f"MCP server '{config.name}' already registered for"
                f" tenant '{config.tenant_id}'"
            )
        tenant_servers[config.name] = config
        logger.info(
            "[MCPRegistry] Registered server %s for tenant %s (scope=%s)",
            config.name, config.tenant_id, config.scope_key,
        )
        self.persist()

    def unregister(self, name: str, tenant_id: str) -> bool:
        """Remove a server registration. Returns True if it existed."""
        tenant_servers = self._servers.get(tenant_id, {})
        if name not in tenant_servers:
            return False
        del tenant_servers[name]
        logger.info(
            "[MCPRegistry] Unregistered server %s for tenant %s", name, tenant_id
        )
        self.persist()
        return True

    def get_for_tenant(self, tenant_id: str) -> list[MCPServerConfig]:
        """List all registered servers for a tenant."""
        return list(self._servers.get(tenant_id, {}).values())

    def get_by_scope(self, scope_key: str, tenant_id: str) -> MCPServerConfig | None:
        """Look up the enabled server for a scope key within a tenant."""
        for config in self._servers.get(tenant_id, {}).values():
            if config.scope_key == scope_key and config.enabled:
                return config
        return None

    def persist(self) -> None:
        """Write the registry to JSON (no-op without a persist_path)."""
        if not self._persist_path:
            return
        data: dict[str, Any] = {"servers": {}}
        for tenant_id, servers in self._servers.items():
            data["servers"][tenant_id] = {
                name: config.to_dict() for name, config in servers.items()
            }
        with open(self._persist_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load(self) -> None:
        """Load from JSON, re-validating every entry (tamper resistance).

        Invalid entries are logged and skipped, never trusted.
        """
        if not self._persist_path or not os.path.exists(self._persist_path):
            return
        with open(self._persist_path, encoding="utf-8") as f:
            data = json.load(f)
        for tenant_id, servers in data.get("servers", {}).items():
            for name, cfg in servers.items():
                try:
                    config = MCPServerConfig(
                        name=cfg["name"],
                        server_url=cfg["server_url"],
                        scope_key=cfg["scope_key"],
                        tenant_id=cfg["tenant_id"],
                        auth_type=cfg.get("auth_type", "bearer"),
                        credential_env_var=cfg.get("credential_env_var", ""),
                        enabled=cfg.get("enabled", True),
                        timeout=cfg.get("timeout", 30.0),
                        default_tool=cfg.get("default_tool", ""),
                        default_arguments=cfg.get("default_arguments", {}),
                    )
                    self._validate_config(config)
                except (ValidationError, KeyError) as exc:
                    logger.warning(
                        "[MCPRegistry] Skipping invalid config '%s' on load: %s",
                        name, exc,
                    )
                    continue
                self._servers.setdefault(tenant_id, {})[name] = config


def create_mcp_registry() -> MCPServerRegistry:
    """Build a registry from the environment (MCP_REGISTRY_PATH for persistence)."""
    return MCPServerRegistry(
        persist_path=os.environ.get("MCP_REGISTRY_PATH", "") or None
    )
