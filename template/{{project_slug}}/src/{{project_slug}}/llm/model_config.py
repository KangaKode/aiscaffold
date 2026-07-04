"""
Model tiering configuration -- vanilla, env-configurable.

Four cost tiers (cheapest to most capable): nano, budget, standard,
frontier. Each tier lists candidate model names, cheapest first. The
router (model_router.py) maps a call role to a tier and picks a model from
that tier, cascading to a more capable tier on failure and downgrading a
tier when a tenant's budget is nearly spent.

The default map below is an EXAMPLE with placeholder-ish model names as of
early 2026 -- it is meant to be replaced, not trusted as a price sheet.
Two ways to override without editing code:

  MODEL_TIER_MAP_JSON='{"nano": ["my-cheap-model"], "standard": ["my-model"]}'
  ROLE_TIER_MAP_JSON='{"chat_synthesis": "standard", "my_role": "budget"}'

Anything unset falls back to the defaults here. Unknown roles resolve to
DEFAULT_TIER, and an empty/invalid config falls back to the built-in map
(fail-safe -- routing never crashes a call).
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

TIER_ORDER: list[str] = ["nano", "budget", "standard", "frontier"]
DEFAULT_TIER = "standard"

# EXAMPLE tier -> candidate models (cheapest first). Replace with the models
# your provider(s) actually expose. Names here are illustrative.
DEFAULT_TIER_MODELS: dict[str, list[str]] = {
    "nano": ["gpt-5-nano", "gemini-2.5-flash-lite", "claude-haiku-4-5"],
    "budget": ["gpt-5-mini", "gemini-2.5-flash"],
    "standard": ["claude-sonnet-4-6", "gpt-5", "gemini-2.5-pro"],
    "frontier": ["claude-opus-4-6"],
}

# EXAMPLE role -> tier heuristics. Cheap tiers for votes and short
# resolutions; capable tiers for analysis, synthesis, and security
# screening. Roles not listed use prefix/suffix rules in the router, then
# DEFAULT_TIER.
DEFAULT_ROLE_TIER: dict[str, str] = {
    "single_shot_resolution": "nano",
    "premise_validation": "nano",
    "chat_cross_check": "budget",
    "cross_check": "budget",
    "chat_synthesis": "budget",
    "enforcement_rewrite": "budget",
    "sentinel_challenge": "budget",
    "synthesis": "standard",
    "specialist": "standard",
    "sentinel_analysis": "frontier",
}

# Short prompts (few tokens) can be served by the cheapest tier.
SHORT_PROMPT_CHARS = 2000


def _load_json_env(var: str) -> dict | None:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        logger.warning("[ModelConfig] %s is not a JSON object; ignoring", var)
    except json.JSONDecodeError:
        logger.warning("[ModelConfig] %s is not valid JSON; ignoring", var)
    return None


def get_tier_models() -> dict[str, list[str]]:
    """Resolve the tier -> models map (env override merged over defaults)."""
    merged = {tier: list(models) for tier, models in DEFAULT_TIER_MODELS.items()}
    override = _load_json_env("MODEL_TIER_MAP_JSON")
    if override:
        for tier, models in override.items():
            if tier in TIER_ORDER and isinstance(models, list) and models:
                merged[tier] = [str(m) for m in models]
    return merged


def get_role_tier_map() -> dict[str, str]:
    """Resolve the role -> tier map (env override merged over defaults)."""
    merged = dict(DEFAULT_ROLE_TIER)
    override = _load_json_env("ROLE_TIER_MAP_JSON")
    if override:
        for role, tier in override.items():
            if tier in TIER_ORDER:
                merged[str(role)] = tier
            else:
                logger.warning("[ModelConfig] Unknown tier '%s' for role '%s'", tier, role)
    return merged


def models_in_tier(tier: str) -> list[str]:
    """Candidate models for a tier (empty list for an unknown tier)."""
    return get_tier_models().get(tier, [])
