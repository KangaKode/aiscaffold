"""
Persistence helpers for AgentRegistry -- load/save remote registrations.

Security invariants:
  - Raw identity tokens are NEVER persisted (hash only).
  - API keys come from environment variables, never from the JSON file.
  - visibility / tenant_id are validated on load so a tampered persistence
    file cannot silently widen an agent's audience: entries with invalid
    metadata are skipped entirely rather than falling back to "public".
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

from ..security import (
    ValidationError,
    validate_identifier,
    validate_in_choices,
    validate_url,
)
from .capability import AgentCapability
from .remote import RemoteAgent

logger = logging.getLogger(__name__)

VISIBILITY_CHOICES = ["public", "team", "private"]


def load_remote_entries(persist_path: Path) -> list[dict[str, Any]]:
    """Load persisted remote agent registrations from disk.

    Returns a list of AgentEntry keyword-argument dicts (the registry
    constructs the entries to avoid a circular import).

    API keys are loaded from environment variables (AGENT_{NAME}_API_KEY),
    never from the JSON file, and only when AGENT_{NAME}_BASE_URL matches
    the persisted base_url.

    Identity tokens: only the token HASH is ever persisted. Loaded remote
    agents therefore get identity_token=None and must re-authenticate by
    rotating credentials (POST /agents/{name}/credentials/rotate) before
    they pass identity verification at dispatch.
    """
    if not persist_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with open(persist_path) as f:
            data = json.load(f)
        for entry in data.get("remote_agents", []):
            try:
                entries.append(_entry_kwargs_from_dict(entry))
            except (KeyError, TypeError, ValidationError) as e:
                logger.warning(
                    f"[AgentRegistry] Skipping invalid persisted agent: {e}"
                )
        logger.info(
            f"[AgentRegistry] Loaded {len(entries)} "
            f"remote agents from {persist_path}"
        )
    except Exception as e:
        logger.warning(f"[AgentRegistry] Failed to load agents: {e}")
    return entries


def _entry_kwargs_from_dict(entry: dict) -> dict[str, Any]:
    """Validate one persisted registration and build AgentEntry kwargs.

    Raises KeyError / TypeError / ValidationError on malformed entries.
    """
    name = validate_identifier(entry["name"], "agent name")
    base_url = validate_url(entry["base_url"], "base_url")
    visibility = validate_in_choices(
        entry.get("visibility", "public"), VISIBILITY_CHOICES, "visibility"
    )
    tenant_id = validate_identifier(entry.get("tenant_id", "default"), "tenant_id")

    api_key_env = f"AGENT_{name.upper()}_API_KEY"
    base_url_env = f"AGENT_{name.upper()}_BASE_URL"
    expected_base_url = os.environ.get(base_url_env, "").rstrip("/")
    api_key = (
        os.environ.get(api_key_env, "")
        if expected_base_url == base_url.rstrip("/")
        else ""
    )

    agent = RemoteAgent(
        name=name,
        domain=entry["domain"],
        base_url=base_url,
        api_key=api_key,
        timeout=entry.get("timeout", 120),
    )
    capability = None
    cap_data = entry.get("capability")
    if isinstance(cap_data, dict):
        capability = AgentCapability(
            access_scopes=cap_data.get("access_scopes", []),
            is_meta_agent=cap_data.get("is_meta_agent", False),
            max_calls_per_hour=cap_data.get("max_calls_per_hour", 100),
        )
    return {
        "agent": agent,
        "agent_type": "remote",
        "capabilities": entry.get("capabilities", []),
        "visibility": visibility,
        "tenant_id": tenant_id,
        "identity_token": None,  # raw token never persisted; rotate to re-issue
        "identity_token_hash": entry.get("identity_token_hash"),
        "suspended": entry.get("suspended", False),
        "capability": capability,
        "last_active": entry.get("last_active"),
        # Expiry metadata (not a secret): lets operators see when the
        # persisted credential dies even before rotating.
        "identity_expires_at": entry.get("identity_expires_at"),
    }


def save_remote_entries(persist_path: Path, entries: list[Any]) -> None:
    """Persist remote agent registrations to disk.

    ``entries`` is a list of AgentEntry-like objects (duck-typed).

    Security: the raw identity token is NEVER written to disk -- only its
    SHA-256 hash, plus visibility/tenant metadata, the suspended flag,
    capability data, and last_active.
    """
    remote_entries = []
    for entry in entries:
        if entry.agent_type == "remote" and hasattr(entry.agent, "to_dict"):
            agent_data = entry.agent.to_dict()
            agent_data["capabilities"] = entry.capabilities
            agent_data["visibility"] = entry.visibility
            agent_data["tenant_id"] = entry.tenant_id
            agent_data["identity_token_hash"] = entry.identity_token_hash
            agent_data["suspended"] = entry.suspended
            agent_data["last_active"] = entry.last_active
            agent_data["identity_expires_at"] = entry.identity_expires_at
            if entry.capability is not None:
                agent_data["capability"] = {
                    "access_scopes": entry.capability.access_scopes,
                    "is_meta_agent": entry.capability.is_meta_agent,
                    "max_calls_per_hour": entry.capability.max_calls_per_hour,
                }
            remote_entries.append(agent_data)

    persist_path.parent.mkdir(parents=True, exist_ok=True)
    with open(persist_path, "w") as f:
        json.dump({"remote_agents": remote_entries}, f, indent=2)
    logger.debug(
        f"[AgentRegistry] Saved {len(remote_entries)} remote agents"
    )
