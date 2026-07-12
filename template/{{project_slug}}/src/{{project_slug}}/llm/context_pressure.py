"""
Detect-only context-pressure signal for LLM calls (opt-in, default off;
detect-never-act: the prompt is never blocked, compacted, or altered).
Two independent conditions, both evaluated on every call when
CONTEXT_PRESSURE_ENABLED is truthy:

  window           -- aggregate estimate (chars // 4: a trend heuristic,
    NOT a token count; +/-30-50% off for code-heavy or CJK content) at
    or over CONTEXT_PRESSURE_THRESHOLD (default 0.8) of
    CONTEXT_PRESSURE_WINDOW_TOKENS (default 50_000). Makes NO claim to
    detect the client's truncation -- that is condition 2's job.
  field_truncation -- any single field STRICTLY over field_cap (the
    caller passes LLMClient._per_field_cap, the client's real per-field
    truncation point): _sanitize_prompt WILL truncate that field next.
    Honest edge: sanitize strips null bytes before ITS length check, so
    a field over the cap only via \\x00 bytes fires without truncating
    (rare false positive, never a false negative).

Each detection increments context_pressure_total{phase, reason} on EVERY
event (the counter is the trend signal) and warns AT MOST ONCE per
(phase, reason) per process (the log is a first-occurrence breadcrumb).
Sizes and phase only -- prompt content is never logged. Any failure is a
debug-logged no-op; the call is never delayed, blocked, or raised at.

Keep this file under 150 lines.
"""

import logging
import os
from dataclasses import dataclass

from ..observability.metrics import PHASES, record_context_pressure  # noqa: F401

logger = logging.getLogger(__name__)

CONTEXT_PRESSURE_ENV = "CONTEXT_PRESSURE_ENABLED"
CONTEXT_PRESSURE_WINDOW_ENV = "CONTEXT_PRESSURE_WINDOW_TOKENS"
CONTEXT_PRESSURE_THRESHOLD_ENV = "CONTEXT_PRESSURE_THRESHOLD"
CHARS_PER_TOKEN = 4  # cheap heuristic; no tokenizer on the hot path
DEFAULT_WINDOW_TOKENS = 50_000
DEFAULT_THRESHOLD = 0.8
_TRUTHY = ("true", "1", "yes")

# Exact role -> phase table; wins BEFORE suffix matching so chat_synthesis
# lands in "chat", never "synthesis". enforcement_rewrite, specialist and
# the default "assistant" are INTENTIONALLY unlisted -> "other".
_ROLE_PHASES = {
    "synthesis": "synthesis",
    "chat_synthesis": "chat",
    "cross_check": "chat",
    "premise_validation": "premise",
    "single_shot_resolution": "single_shot",
}
_SUFFIX_PHASES = (("_analysis", "analysis"), ("_challenge", "challenge"),
                  ("_vote", "vote"))

# Warn-once bookkeeping: (phase, reason) keys, bounded by 8 x 2 = 16.
_warned: set[tuple[str, str]] = set()


def _reset_warned() -> None:
    """Test hook: clear the once-per-process warn bookkeeping."""
    _warned.clear()


@dataclass(frozen=True)
class PressureFlags:
    """Resolved CONTEXT_PRESSURE_* configuration (off in the default)."""

    enabled: bool = False
    window_tokens: int = DEFAULT_WINDOW_TOKENS
    threshold: float = DEFAULT_THRESHOLD


def resolve_pressure_flags() -> PressureFlags:
    """Parse the CONTEXT_PRESSURE_* env vars; garbage or out-of-range
    numerics fall back to the documented defaults."""
    enabled = os.environ.get(CONTEXT_PRESSURE_ENV, "").strip().lower() in _TRUTHY
    try:
        window = int(os.environ.get(CONTEXT_PRESSURE_WINDOW_ENV, "") or 0)
    except ValueError:
        window = 0
    if window <= 0:
        window = DEFAULT_WINDOW_TOKENS
    try:
        threshold = float(os.environ.get(CONTEXT_PRESSURE_THRESHOLD_ENV, "") or 0)
    except ValueError:
        threshold = 0.0
    if not 0 < threshold <= 1:
        threshold = DEFAULT_THRESHOLD
    return PressureFlags(enabled=enabled, window_tokens=window, threshold=threshold)


def phase_for_role(role) -> str:
    """Bucket a caller-supplied role into the bounded PHASES enum (exact
    table first, then suffix, else "other") -- arbitrary role strings can
    never explode label cardinality."""
    if not role or not isinstance(role, str):
        return "other"
    if role in _ROLE_PHASES:
        return _ROLE_PHASES[role]
    for suffix, phase in _SUFFIX_PHASES:
        if role.endswith(suffix):
            return phase
    return "other"


def estimated_tokens(prompt) -> int:
    """Aggregate size estimate: total chars // CHARS_PER_TOKEN."""
    return prompt.total_length // CHARS_PER_TOKEN


def check_context_pressure(prompt, role, field_cap: int) -> None:
    """Evaluate both pressure conditions for one LLM call (see module
    docstring). Flag off: one env read, prompt untouched. Never raises."""
    if os.environ.get(CONTEXT_PRESSURE_ENV, "").strip().lower() not in _TRUTHY:
        return
    try:
        flags = resolve_pressure_flags()
        phase = phase_for_role(role)
        est = estimated_tokens(prompt)
        if est >= flags.threshold * flags.window_tokens:
            _emit(phase, "window",
                  f"estimated {est} tokens >= {flags.threshold:.2f} x "
                  f"{flags.window_tokens}-token window")
        over = [
            name for name in ("system", "context", "user_message")
            if len(getattr(prompt, name)) > field_cap
        ]
        if over:
            _emit(phase, "field_truncation",
                  f"field(s) {over} over the {field_cap}-char per-field cap "
                  "-- the client will truncate them")
    except Exception as exc:
        logger.debug(f"[ContextPressure] check failed (non-fatal): {exc}")


def _emit(phase: str, reason: str, detail: str) -> None:
    """Counter on every event; warning once per (phase, reason) per process."""
    record_context_pressure(phase, reason)
    if (phase, reason) in _warned:
        return
    _warned.add((phase, reason))
    logger.warning(
        f"[ContextPressure] {reason} in phase '{phase}': {detail} "
        "(detect-only; logged once per process -- the "
        "context_pressure_total counter tracks every event)"
    )
