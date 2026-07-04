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


class RoundTableResultResponse(BaseModel):
    """Complete round table output returned to the client."""

    task_id: str
    status: str = "completed"
    consensus_reached: bool = False
    approval_rate: float = 0.0
    analyses: list[AnalysisResponse] = Field(default_factory=list)
    challenges: list[ChallengeResponse] = Field(default_factory=list)
    synthesis: SynthesisResponse | None = None
    votes: list[VoteResponse] = Field(default_factory=list)
    duration_seconds: float = 0.0
    degraded: bool = False  # Quorum of domain-agent analyses not met
    failed_agent_count: int = 0  # Agents skipped by dispatch gates or failed


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
    mode: str = "sync"
    visibility: str = "public"
    tenant_id: str = "default"
    healthy: bool = True
    interaction_count: int = 0
    suspended: bool = False
    credential_status: str = "none"  # "active" if a token hash is stored
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
