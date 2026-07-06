"""Per-agent call rate limiter.

In-memory sliding window rate limiter keyed by agent name.
Standalone module so orchestrators and the registry can share one instance.
"""

import logging
import time

logger = logging.getLogger(__name__)

DEFAULT_MAX_CALLS_PER_HOUR = 100
WINDOW_SECONDS = 3600.0


class AgentRateLimiter:
    """In-memory per-agent-name call rate limiter.

    Tracks call timestamps per agent name in a sliding 1-hour window.
    Orchestration dispatch checks before calling each agent.

    Known limitations:
    - Counters reset on process restart
    - Per-name keying: re-registering with different names creates separate counters
    - No cross-instance coordination (single-process): N replicas grant
      each agent N x max_calls_per_hour in aggregate -- see
      docs/OPERATIONS.md ("Rate limiting is per-process")
    """

    def __init__(self) -> None:
        self._call_times: dict[str, list[float]] = {}

    def check_and_record(
        self,
        agent_name: str,
        max_calls: int = DEFAULT_MAX_CALLS_PER_HOUR,
        now: float | None = None,
    ) -> bool:
        """Check if agent is within rate limit and record the call if allowed.

        Args:
            agent_name: Agent name (rate limit key).
            max_calls: Maximum calls allowed per hour.
            now: Current timestamp (injectable for testing).

        Returns:
            True if allowed (call recorded), False if rate limited.
        """
        if now is None:
            now = time.monotonic()

        times = self._call_times.get(agent_name, [])
        cutoff = now - WINDOW_SECONDS
        times = [t for t in times if t > cutoff]

        if len(times) >= max_calls:
            self._call_times[agent_name] = times
            return False

        times.append(now)
        self._call_times[agent_name] = times
        return True

    def is_rate_limited(
        self,
        agent_name: str,
        max_calls: int = DEFAULT_MAX_CALLS_PER_HOUR,
        now: float | None = None,
    ) -> bool:
        """Check if agent is currently over the rate limit (read-only).

        Does not record a call. Used by orchestrators for pre-check.
        """
        if now is None:
            now = time.monotonic()
        times = self._call_times.get(agent_name, [])
        cutoff = now - WINDOW_SECONDS
        active = [t for t in times if t > cutoff]
        return len(active) >= max_calls

    def cleanup(self, now: float | None = None) -> None:
        """Remove call records older than WINDOW_SECONDS."""
        if now is None:
            now = time.monotonic()

        cutoff = now - WINDOW_SECONDS
        empty_keys: list[str] = []
        for name, times in self._call_times.items():
            pruned = [t for t in times if t > cutoff]
            if pruned:
                self._call_times[name] = pruned
            else:
                empty_keys.append(name)

        for key in empty_keys:
            del self._call_times[key]
