"""
Pydantic request models -- the API contract for external agents.

Any language can implement these 3 endpoints:
  POST /analyze   -> AnalyzeRequest
  POST /challenge -> ChallengeRequest
  POST /vote      -> VoteRequest

These mirror the AgentProtocol from orchestration/round_table.py over HTTP.
"""

from pydantic import BaseModel, Field, field_validator


# =============================================================================
# ROUND TABLE TASK SUBMISSION
# =============================================================================


class TaskClaimModel(BaseModel):
    """One Ideal State claim for optional Task ISA."""

    id: str = Field(..., min_length=1, max_length=64)
    statement: str = Field(..., min_length=1, max_length=500)
    evidence_kind: str = Field(
        default="citation",
        description="tool_result | citation | artifact | human_ack",
    )
    required: bool = True

    @field_validator("evidence_kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        allowed = {"tool_result", "citation", "artifact", "human_ack"}
        if v not in allowed:
            raise ValueError(f"evidence_kind must be one of {sorted(allowed)}")
        return v


class TaskISAModel(BaseModel):
    """Optional Ideal State Artifact (definition of done) for a task."""

    version: str = "1"
    ideal_summary: str = Field(default="", max_length=500)
    claims: list[TaskClaimModel] = Field(default_factory=list, max_length=32)

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if v != "1":
            raise ValueError("version must be '1'")
        return v

    @field_validator("claims")
    @classmethod
    def _unique_ids(cls, claims: list[TaskClaimModel]) -> list[TaskClaimModel]:
        ids = [c.id for c in claims]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate claim id")
        return claims


class RoundTableTaskRequest(BaseModel):
    """Submit a task to the round table for multi-agent analysis."""

    content: str = Field(..., description="The task content for agents to analyze")
    context: dict = Field(default_factory=dict, description="Additional context")
    constraints: list[str] = Field(default_factory=list, description="Task constraints")
    agent_ids: list[str] | None = Field(
        None, description="Specific agents to include (None = all registered)"
    )
    config_overrides: dict = Field(
        default_factory=dict,
        description="Override round table config (e.g., consensus_threshold)",
    )
    isa: TaskISAModel | None = Field(
        default=None,
        description="Optional Ideal State Artifact; detect-only claim closure",
    )


# =============================================================================
# AGENT PROTOCOL REQUESTS (sent TO external agents)
# =============================================================================


class Observation(BaseModel):
    """A single finding with evidence."""

    finding: str
    evidence: str
    severity: str = "info"
    confidence: float = 0.5


class Recommendation(BaseModel):
    """A recommended action with rationale."""

    action: str
    rationale: str
    priority: str = "medium"


class AnalyzeRequest(BaseModel):
    """Sent to an agent's /analyze endpoint."""

    task_id: str
    content: str
    context: dict = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)


class ChallengeRequest(BaseModel):
    """Sent to an agent's /challenge endpoint."""

    task_id: str
    content: str
    other_analyses: list[dict] = Field(
        default_factory=list,
        description="Other agents' analyses to challenge",
    )


class VoteRequest(BaseModel):
    """Sent to an agent's /vote endpoint."""

    task_id: str
    content: str
    synthesis: dict = Field(
        default_factory=dict,
        description="The orchestrator's synthesis to vote on",
    )


# =============================================================================
# AGENT REGISTRATION
# =============================================================================


class AgentRegistration(BaseModel):
    """Register an external agent with the system."""

    name: str = Field(..., description="Unique agent name")
    domain: str = Field(..., description="Agent's area of expertise")
    base_url: str = Field(..., description="Base URL for the agent's API endpoints")
    api_key: str = Field("", description="API key for authenticating with the agent")
    capabilities: list[str] = Field(
        default_factory=list, description="List of capability tags"
    )
    access_scopes: list[str] = Field(
        default_factory=list,
        description="Task-context keys the agent may access (empty = unrestricted)",
    )
    max_calls_per_hour: int | None = Field(
        None, description="Per-agent rate limit (None = platform default)"
    )
    is_meta_agent: bool = Field(
        False, description="Meta-agents also receive the peer_analyses context key"
    )
    visibility: str = Field(
        "public",
        description="Who can see this agent: 'public' (all tenants), "
        "'team' (registering tenant only), or 'private'",
    )


# =============================================================================
# SESSION MANAGEMENT
# =============================================================================


class CreateSessionRequest(BaseModel):
    """Create a new session thread."""

    metadata: dict = Field(default_factory=dict)


class AddTurnRequest(BaseModel):
    """Add a turn to an existing session."""

    content: str = Field(..., description="User input for this turn")
    metadata: dict = Field(default_factory=dict)


