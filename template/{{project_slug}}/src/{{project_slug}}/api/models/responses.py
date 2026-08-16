"""
Pydantic response models -- what the API returns.

These are the shapes that external agents return from their endpoints
and that the gateway returns to clients.
"""

from pydantic import BaseModel, Field


# =============================================================================
# AGENT PROTOCOL RESPONSES (returned FROM external agents)
# =============================================================================


class AnalysisResponse(BaseModel):
    """Returned by an agent's /analyze endpoint."""

    agent_name: str
    domain: str
    observations: list[dict] = Field(default_factory=list)
    recommendations: list[dict] = Field(default_factory=list)
    confidence: float = 0.0


class ChallengeResponse(BaseModel):
    """Returned by an agent's /challenge endpoint."""

    agent_name: str
    challenges: list[dict] = Field(default_factory=list)
    concessions: list[dict] = Field(default_factory=list)


class VoteResponse(BaseModel):
    """Returned by an agent's /vote endpoint."""

    agent_name: str
    approve: bool = False
    conditions: list[str] = Field(default_factory=list)
    dissent_reason: str | None = None


# =============================================================================
# ROUND TABLE RESULT
# =============================================================================


class SynthesisResponse(BaseModel):
    """Synthesis from the orchestrator."""

    recommended_direction: str = ""
    key_findings: list[dict] = Field(default_factory=list)
    trade_offs: list[str] = Field(default_factory=list)
    minority_views: list[dict] = Field(default_factory=list)


class PremiseChallengeResponse(BaseModel):
    """Phase 0.5 refusal gate outcome: the agents declined the task."""

    premise_challenged: bool = True
    what_is_wrong: str = ""
    what_is_missing: str = ""
    better_question: str = ""
    refusing_agents: list[str] = Field(default_factory=list)
    agents_who_would_proceed: list[str] = Field(default_factory=list)


class ClaimClosureResponse(BaseModel):
    """One claim's detect-only closure status."""

    claim_id: str
    status: str  # closed | open | unverifiable
    detail: str = ""


class IsaClosureReportResponse(BaseModel):
    """Detect-only ISA closure; does not refuse consensus in Phase 1."""

    claim_closures: list[ClaimClosureResponse] = Field(default_factory=list)
    all_required_closed: bool = False
    error: str | None = None


class RoundTableResultResponse(BaseModel):
    """Complete round table output returned to the client.

    status is "refused" when a gate short-circuited the deliberation:
    refusal_source says which one ("premise_gate" with premise_challenge
    populated, "sentinel" when opt-in Sentinel enforcement tripped, or
    "canary" when opt-in runtime canary enforcement tripped) and
    refusal_reason carries the machine-readable reason
    ("premise_challenged", a SentinelRefusal reason such as
    "sentinel_high_risk" / "sentinel_unavailable" / "sentinel_premise" /
    "sentinel_missing", or "canary_leak").
    """

    task_id: str
    status: str = "completed"
    refusal_source: str | None = None
    refusal_reason: str | None = None
    premise_challenge: PremiseChallengeResponse | None = None
    consensus_reached: bool = False
    approval_rate: float = 0.0
    analyses: list[AnalysisResponse] = Field(default_factory=list)
    challenges: list[ChallengeResponse] = Field(default_factory=list)
    synthesis: SynthesisResponse | None = None
    votes: list[VoteResponse] = Field(default_factory=list)
    duration_seconds: float = 0.0
    degraded: bool = False  # Analysis quorum not met, or voters gated out at Phase 3
    failed_agent_count: int = 0  # Agents skipped by dispatch gates or failed
    vote_gated_count: int = 0  # Voters excluded by Phase 3 dispatch gates
    isa_closure: IsaClosureReportResponse | None = None


# =============================================================================
# AGENT REGISTRY
# =============================================================================


class AgentInfo(BaseModel):
    """Information about a registered agent."""

    name: str
    domain: str
    agent_type: str = "local"
    base_url: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    visibility: str = "public"
    tenant_id: str = "default"
    healthy: bool = True
    interaction_count: int = 0
    suspended: bool = False
    credential_status: str = "none"  # "active" if a token hash is stored
    # Identity-token expiry (ISO 8601 UTC); rotate before this passes or
    # the agent is blocked at dispatch. None when no token is held.
    expires_at: str | None = None
    last_active: str | None = None
    dormant: bool = False


class AgentListResponse(BaseModel):
    """List of all registered agents."""

    agents: list[AgentInfo] = Field(default_factory=list)
    total: int = 0


# =============================================================================
# SESSION
# =============================================================================


class SessionResponse(BaseModel):
    """Session thread state."""

    session_id: str
    status: str = "active"
    turn_count: int = 0
    created_at: str = ""
    metadata: dict = Field(default_factory=dict)


# =============================================================================
# HEALTH
# =============================================================================


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "healthy"
    version: str = "0.1.0"
    agents_registered: int = 0
    agents_healthy: int = 0
    uptime_seconds: float = 0.0


class ReadinessResponse(BaseModel):
    """Readiness check response (deeper than health)."""

    ready: bool = True
    checks: dict = Field(default_factory=dict)


class MetricsResponse(BaseModel):
    """Basic operational metrics."""

    tasks_completed: int = 0
    tasks_failed: int = 0
    average_duration_seconds: float = 0.0
    agents_registered: int = 0
    total_agent_calls: int = 0


# =============================================================================
# COMMON
# =============================================================================


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    detail: str = ""
    status_code: int = 500
