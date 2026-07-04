"""Agent capability declaration for scope filtering and rate limiting."""

from dataclasses import dataclass, field

from .rate_limiter import DEFAULT_MAX_CALLS_PER_HOUR


@dataclass(frozen=True)
class AgentCapability:
    """Declares what data an agent may access and how often it may be called.

    Scopes semantics: ``access_scopes`` lists the task-context keys the agent
    is authorized to read (and the data sources it may cite in findings).
    An EMPTY list means unrestricted access -- this is the zero-config
    default so existing agents keep working without declaring capabilities.

    ``is_meta_agent`` marks agents that synthesize peer output (they always
    receive the ``peer_analyses`` context key in addition to their scopes).
    """

    access_scopes: list[str] = field(default_factory=list)
    is_meta_agent: bool = False
    max_calls_per_hour: int = DEFAULT_MAX_CALLS_PER_HOUR
