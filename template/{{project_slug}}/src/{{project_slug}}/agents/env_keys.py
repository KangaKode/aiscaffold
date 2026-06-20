"""Environment variable key helpers for remote agents."""

import hashlib
import re


def agent_env_prefix(name: str) -> str:
    """Return a portable, collision-resistant env var prefix for an agent name."""
    if name.isascii() and name.isidentifier():
        return f"AGENT_{name.upper()}"

    safe_name = re.sub(r"[^A-Z0-9_]", "_", name.upper()).strip("_") or "REMOTE"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8].upper()
    return f"AGENT_{safe_name}_{digest}"
