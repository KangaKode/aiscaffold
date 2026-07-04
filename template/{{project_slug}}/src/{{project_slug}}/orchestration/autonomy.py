"""
Graduated autonomy -- maps a trust level (1-6) to operating limits.

Level 1 is the most trusted (no approval gate, more specialists, higher
rate allowance); level 6 is the most restricted (human approval required,
one specialist, quarter rate, auto-escalate on any conflict). Unknown or
invalid levels always resolve to the MOST RESTRICTIVE policy so a bad
input can never grant extra freedom (fail-safe).

Deployments can tune individual fields per level via the AUTONOMY_POLICIES
environment variable -- a JSON object mapping level to partial policy
fields, e.g.:

    AUTONOMY_POLICIES='{"2": {"max_specialists": 3}, "4": {"rate_limit_multiplier": 1.0}}'

The variable is parsed once per process. Malformed JSON, unknown fields,
or invalid values are logged and ignored (defaults win).

Keep this file under 500 lines.
"""

import json
import logging
import os
from dataclasses import dataclass, replace

logger = logging.getLogger(__name__)

MOST_RESTRICTIVE_LEVEL = 6


@dataclass(frozen=True)
class AutonomyPolicy:
    """Operating limits for one autonomy level. Lower level = more trusted."""

    level: int
    require_human_approval: bool
    max_specialists: int
    rate_limit_multiplier: float
    auto_escalate_on_conflict: bool


DEFAULT_POLICIES: dict[int, AutonomyPolicy] = {
    1: AutonomyPolicy(
        level=1,
        require_human_approval=False,
        max_specialists=5,
        rate_limit_multiplier=2.0,
        auto_escalate_on_conflict=False,
    ),
    2: AutonomyPolicy(
        level=2,
        require_human_approval=False,
        max_specialists=4,
        rate_limit_multiplier=1.5,
        auto_escalate_on_conflict=False,
    ),
    3: AutonomyPolicy(
        level=3,
        require_human_approval=False,
        max_specialists=3,
        rate_limit_multiplier=1.0,
        auto_escalate_on_conflict=True,
    ),
    4: AutonomyPolicy(
        level=4,
        require_human_approval=True,
        max_specialists=2,
        rate_limit_multiplier=0.75,
        auto_escalate_on_conflict=True,
    ),
    5: AutonomyPolicy(
        level=5,
        require_human_approval=True,
        max_specialists=1,
        rate_limit_multiplier=0.5,
        auto_escalate_on_conflict=True,
    ),
    6: AutonomyPolicy(
        level=6,
        require_human_approval=True,
        max_specialists=1,
        rate_limit_multiplier=0.25,
        auto_escalate_on_conflict=True,
    ),
}

# Overridable fields and their expected types. "level" is intentionally
# absent -- a policy's level can never be changed by an override.
_FIELD_TYPES: dict[str, type | tuple] = {
    "require_human_approval": bool,
    "max_specialists": int,
    "rate_limit_multiplier": (int, float),
    "auto_escalate_on_conflict": bool,
}

_env_overrides_cache: dict[int, dict] | None = None


def _parse_env_overrides(raw: str) -> dict[int, dict]:
    """Parse AUTONOMY_POLICIES JSON into {level: partial_fields}."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[Autonomy] AUTONOMY_POLICIES is not valid JSON -- ignoring")
        return {}
    if not isinstance(data, dict):
        logger.warning("[Autonomy] AUTONOMY_POLICIES must be a JSON object -- ignoring")
        return {}

    parsed: dict[int, dict] = {}
    for key, partial in data.items():
        try:
            level = int(key)
        except (TypeError, ValueError):
            logger.warning(
                f"[Autonomy] Ignoring non-integer level {key!r} in AUTONOMY_POLICIES"
            )
            continue
        if not isinstance(partial, dict):
            logger.warning(
                f"[Autonomy] Ignoring level {level}: override must be an object"
            )
            continue
        parsed[level] = partial
    return parsed


def _env_overrides() -> dict[int, dict]:
    """Environment overrides, parsed once per process."""
    global _env_overrides_cache
    if _env_overrides_cache is None:
        _env_overrides_cache = _parse_env_overrides(
            os.environ.get("AUTONOMY_POLICIES", "")
        )
    return _env_overrides_cache


def _apply_partial(
    policy: AutonomyPolicy, partial: dict, source: str
) -> AutonomyPolicy:
    """Apply validated partial field overrides to a policy."""
    changes: dict = {}
    for name, value in partial.items():
        expected = _FIELD_TYPES.get(name)
        if expected is None:
            logger.warning(f"[Autonomy] Unknown policy field {name!r} in {source}")
            continue
        if expected is bool:
            if not isinstance(value, bool):
                logger.warning(f"[Autonomy] {name} must be a boolean ({source})")
                continue
            changes[name] = value
        elif expected is int:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                logger.warning(
                    f"[Autonomy] {name} must be a non-negative integer ({source})"
                )
                continue
            changes[name] = value
        else:  # float-like
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                logger.warning(f"[Autonomy] {name} must be a positive number ({source})")
                continue
            changes[name] = float(value)
    return replace(policy, **changes) if changes else policy


def resolve_policy(level: int, overrides: dict | None = None) -> AutonomyPolicy:
    """
    Resolve the effective policy for an autonomy level.

    Precedence: DEFAULT_POLICIES < AUTONOMY_POLICIES env var < explicit
    overrides argument. Unknown/invalid levels resolve to the most
    restrictive policy (fail-safe).
    """
    policy = DEFAULT_POLICIES.get(level) if isinstance(level, int) else None
    if policy is None:
        logger.warning(
            f"[Autonomy] Unknown autonomy level {level!r} -- "
            f"falling back to most restrictive (level {MOST_RESTRICTIVE_LEVEL})"
        )
        policy = DEFAULT_POLICIES[MOST_RESTRICTIVE_LEVEL]

    env_partial = _env_overrides().get(policy.level)
    if env_partial:
        policy = _apply_partial(policy, env_partial, "AUTONOMY_POLICIES")
    if overrides:
        policy = _apply_partial(policy, overrides, "overrides")
    return policy
