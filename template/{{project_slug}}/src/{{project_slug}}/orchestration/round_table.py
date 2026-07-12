"""
Generic Round Table Protocol - Multi-agent coordination in phases.

Phase 0.5: PREMISE  -- Agents may collectively refuse a flawed task (cheap gate)
Phase 0: STRATEGY   -- Orchestrator plans before dispatching (extended thinking)
Phase 1: INDEPENDENT -- Agents analyze in parallel (prevent groupthink)
Phase 2: CHALLENGE   -- Cross-agent questioning with evidence (mediated hub-and-spoke)
Phase 3: SYNTHESIS   -- Consensus building with preserved minority views + voting

Key design principles from 2026 research:
- Hub-and-spoke: agents report to orchestrator, never to each other directly
- Filesystem intermediary: all results written to artifacts/ (no game of telephone)
- Evidence preservation: synthesis NEVER drops evidence fields from agent outputs
- Human-in-the-loop: consensus can require human approval before proceeding
- Separate context windows: each agent gets its own LLM call (80% of performance)

Reference: docs/REFERENCES.md
"""

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..observability.tracing import phase_span

logger = logging.getLogger(__name__)


# =============================================================================
# AGENT PROTOCOL
# =============================================================================


@runtime_checkable
class AgentProtocol(Protocol):
    """Interface any agent must implement to participate in a round table.

    Example:
        class MyAgent:
            name = "analyst"
            domain = "data analysis"

            async def analyze(self, task): ...
            async def challenge(self, task, other_analyses): ...
            async def vote(self, task, synthesis): ...
    """

    @property
    def name(self) -> str: ...

    @property
    def domain(self) -> str: ...

    async def analyze(self, task: "RoundTableTask") -> "AgentAnalysis": ...

    async def challenge(
        self, task: "RoundTableTask", other_analyses: list["AgentAnalysis"]
    ) -> "AgentChallenge": ...

    async def vote(
        self, task: "RoundTableTask", synthesis: "SynthesisResult"
    ) -> "AgentVote": ...


# =============================================================================
# DATA MODELS
# =============================================================================


@dataclass
class RoundTableTask:
    """Input to a round table session.

    tenant_id: tenant attribution for the opt-in detection hooks. Additive.
    """

    id: str
    content: str
    context: dict[str, Any] = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    tenant_id: str = "default"


@dataclass
class AgentAnalysis:
    """Phase 1: An agent's independent analysis with evidence."""

    agent_name: str
    domain: str
    observations: list[dict] = field(default_factory=list)
    # Each: {"finding": str, "evidence": str, "severity": str, "confidence": float}
    recommendations: list[dict] = field(default_factory=list)
    # Each: {"action": str, "rationale": str, "priority": str}
    confidence: float = 0.0
    raw_response: str = ""  # Full LLM output preserved for audit
    premise_valid: bool = True  # False when the agent refuses a flawed/unsafe task
    refusal_reason: str = ""


@dataclass
class AgentChallenge:
    """Phase 2: An agent's challenges to other analyses."""

    agent_name: str
    challenges: list[dict] = field(default_factory=list)
    # Each: {"target_agent": str, "finding_challenged": str, "counter_evidence": str}
    concessions: list[dict] = field(default_factory=list)
    # Each: {"target_agent": str, "finding_accepted": str, "reason": str}


@dataclass
class StrategyPlan:
    """Phase 0: Orchestrator's plan before dispatching agents."""

    task_decomposition: list[str] = field(default_factory=list)
    agent_focus_areas: dict[str, str] = field(default_factory=dict)  # agent -> focus
    anticipated_tensions: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    reasoning: str = ""


@dataclass
class SynthesisResult:
    """Phase 3: Orchestrator's synthesis preserving ALL evidence."""

    recommended_direction: str = ""
    key_findings: list[dict] = field(default_factory=list)
    # Each PRESERVES: agent_name, finding, evidence, confidence
    trade_offs: list[str] = field(default_factory=list)
    minority_views: list[dict] = field(default_factory=list)
    # Each: {"agent_name": str, "view": str, "evidence": str}


@dataclass
class AgentVote:
    """Phase 3: An agent's vote on the synthesis."""

    agent_name: str
    approve: bool = False
    conditions: list[str] = field(default_factory=list)
    dissent_reason: str | None = None


@dataclass
class RoundTableResult:
    """Complete round table output.

    degraded/failed_agent_count: quorum tracking. degraded=True when fewer
    domain (non-core) agents than config.min_quorum produced analyses AND
    at least one agent was skipped by a dispatch gate or failed.

    vote_gated_count: voters excluded MID-RUN by the Phase 3 dispatch
    gates (suspension, rate limit) after contributing a Phase 1
    analysis; roster members already excluded before deliberation are
    not counted. Any mid-run vote-phase gate-out also sets
    degraded=True: the approval rate divides by votes actually cast, so
    a silently shrunken voter set must never present as a clean result.
    """

    task_id: str
    premise_challenge: Any = None  # PremiseChallengeResult when the gate tripped
    sentinel_refusal: Any = None  # SentinelRefusal when opt-in enforcement tripped
    strategy: StrategyPlan | None = None
    analyses: list[AgentAnalysis] = field(default_factory=list)
    challenges: list[AgentChallenge] = field(default_factory=list)
    synthesis: SynthesisResult | None = None
    votes: list[AgentVote] = field(default_factory=list)
    consensus_reached: bool = False
    duration_seconds: float = 0.0
    degraded: bool = False
    failed_agent_count: int = 0
    vote_gated_count: int = 0
    requires_approval: bool = False  # Human must approve before acting on this

    @property
    def approval_rate(self) -> float:
        if not self.votes:
            return 0.0
        return sum(1 for v in self.votes if v.approve) / len(self.votes)


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class RoundTableConfig:
    """Configuration for a round table session."""

    enable_strategy_phase: bool = True
    enable_challenge_phase: bool = True
    max_challenge_rounds: int = 1
    consensus_threshold: float = 0.7  # % of agents that must approve
    require_human_approval: bool = False  # Human gate after synthesis
    artifacts_dir: Path = Path(".aiscaffold/artifacts")
    write_artifacts: bool = True
    include_core_agents: bool = True  # Auto-inject Skeptic, Quality, Evidence agents
    enforce_evidence: bool = True  # Run evidence enforcement pipeline on Phase 1 responses
    min_quorum: int = 2  # Min successful domain-agent analyses before result is degraded
    autonomy_level: int | None = None  # 1-6; policy may force human approval
    premise_challenge_enabled: bool = True  # Phase 0.5 refusal gate (needs an LLM)
    refusal_threshold: int = 2  # Agents needed to trip the gate (clamped, see premise.py)
    # Opt-in short-circuit: None = env SENTINEL_ENFORCEMENT_ENABLED (default OFF)
    sentinel_enforcement: bool | None = None


# =============================================================================
# ROUND TABLE ORCHESTRATOR
# =============================================================================


class RoundTable:
    """
    Generic multi-agent round table orchestrator.

    Usage:
        agents = [AnalystAgent(llm), ReviewerAgent(llm), FactCheckerAgent(llm)]
        config = RoundTableConfig()
        rt = RoundTable(agents=agents, config=config, llm_client=llm)
        result = await rt.run(task)

        if result.consensus_reached:
            print("Team agrees:", result.synthesis.recommended_direction)
        else:
            print("Dissent:", [v for v in result.votes if not v.approve])
    """

    def __init__(
        self,
        agents: list,
        config: RoundTableConfig,
        llm_client: Any = None,
        registry: Any = None,
        checkin_manager: Any = None,
        baseline_tracker: Any = None,
        collusion_detector: Any = None,
        learning_store: Any = None,
        delegation_recorder: Any = None,
    ):
        self.config = config
        self.llm = llm_client
        self._registry = registry  # Optional: enables identity/rate-limit gates
        self._checkin_manager = checkin_manager  # Optional: approval check-ins
        # Opt-in detection hooks (None = off, behavior unchanged); build via
        # the env-gated factories in learning/activity, learning/collusion
        # and learning/delegation. learning_store enables the
        # agent_identity_blocked integrity flag at the dispatch gates.
        self._baseline_tracker = baseline_tracker
        self._collusion_detector = collusion_detector
        self._learning_store = learning_store
        self._delegation_recorder = delegation_recorder
        self._core_agent_names: set[str] = set()
        self._sentinel_agent: Any = None

        if config.include_core_agents:
            try:
                from ..agents.core import get_core_agents
                core = get_core_agents(llm_client=llm_client)
                core_names = {a.name for a in core}
                self._core_agent_names = core_names
                self._sentinel_agent = next((a for a in core if a.name == "sentinel"), None)
                user_agents = [a for a in agents if a.name not in core_names]
                self.agents = core + user_agents
                logger.info(
                    f"[RoundTable] Initialized with {len(core)} core + "
                    f"{len(user_agents)} user agents"
                )
            except Exception as e:
                logger.warning(f"[RoundTable] Core agents failed to load: {e}")
                self.agents = agents
                logger.info(f"[RoundTable] Initialized with {len(agents)} agents")
        else:
            self.agents = agents
            logger.info(f"[RoundTable] Initialized with {len(agents)} agents (core agents disabled)")

        # Opt-in Sentinel enforcement (default off; binds to the CORE Sentinel
        # OBJECT -- see round_table_helpers.init_sentinel_enforcement).
        from .round_table_helpers import init_sentinel_enforcement
        (self._sentinel_enforcement,
         self._rate_limit_exempt) = init_sentinel_enforcement(config, self._sentinel_agent)

    async def run(self, task: RoundTableTask) -> RoundTableResult:
        """Execute the full phased round table protocol."""
        start = datetime.now()
        result = RoundTableResult(task_id=task.id)

        # Phase 0.5: Premise validation -- cheap parallel checks BEFORE any
        # expensive phase. Skipped (not failed) without an LLM; Sentinel's
        # per-agent screening remains the fail-closed backstop.
        if self.config.premise_challenge_enabled and self.llm:
            from .premise import phase_premise_validation

            logger.info("[RoundTable] Phase 0.5: Premise validation")
            with phase_span("deliberation.phase.premise", agent_count=len(self.agents)):
                pcr = await phase_premise_validation(
                    self.agents, task, self.llm,
                    refusal_threshold=self.config.refusal_threshold,
                )
            if pcr.premise_challenged:
                result.premise_challenge = pcr
                result.duration_seconds = (datetime.now() - start).total_seconds()
                self._write_artifact(task.id, "phase0_5_premise_challenge", asdict(pcr))
                logger.info(
                    f"[RoundTable] Task refused at premise gate by "
                    f"{len(pcr.challenge_reasons)} agents -- short-circuiting"
                )
                return result

        # Phase 0: Strategy
        if self.config.enable_strategy_phase and self.llm:
            logger.info("[RoundTable] Phase 0: Strategy planning")
            with phase_span("deliberation.phase.strategy"):
                result.strategy = await self._phase_strategy(task)
            self._write_artifact(task.id, "phase0_strategy", asdict(result.strategy))

        # Phase 1: Independent Analysis (PARALLEL -- separate context windows)
        # Wire strategy focus areas into the task context so agents specialize
        if result.strategy and result.strategy.agent_focus_areas:
            task.context["agent_focus_areas"] = result.strategy.agent_focus_areas

        logger.info(f"[RoundTable] Phase 1: Independent analysis ({len(self.agents)} agents)")
        with phase_span("deliberation.phase.independent", agent_count=len(self.agents)):
            (result.analyses, result.failed_agent_count,
             sentinel_analysis) = await self._phase_independent(task)
        self._write_artifact(task.id, "phase1_analyses", [asdict(a) for a in result.analyses])

        # Opt-in Sentinel enforcement (default off): halt on the captured verdict.
        if self._sentinel_enforcement:
            from .round_table_helpers import finalize_sentinel_refusal
            if finalize_sentinel_refusal(result, sentinel_analysis, self._write_artifact):
                result.duration_seconds = (datetime.now() - start).total_seconds()
                return result

        # Quorum: degraded when too few domain (non-core) analyses succeeded
        # AND at least one agent was skipped by a gate or failed.
        domain_ok = sum(
            1 for a in result.analyses
            if a.agent_name not in self._core_agent_names
        )
        if result.failed_agent_count > 0 and domain_ok < self.config.min_quorum:
            result.degraded = True
            logger.warning(
                f"[RoundTable] Degraded result: {domain_ok} domain analyses "
                f"(min_quorum={self.config.min_quorum}, "
                f"{result.failed_agent_count} agents skipped/failed)"
            )

        # Phase 2: Challenge
        if self.config.enable_challenge_phase:
            logger.info("[RoundTable] Phase 2: Cross-agent challenge")
            with phase_span("deliberation.phase.challenge", agent_count=len(self.agents)):
                result.challenges = await self._phase_challenge(task, result.analyses)
            self._write_artifact(task.id, "phase2_challenges", [asdict(c) for c in result.challenges])

        # Phase 3: Synthesis + Voting
        logger.info("[RoundTable] Phase 3: Synthesis + voting")
        from .round_table_helpers import phase_synthesis
        with phase_span("deliberation.phase.synthesis"):
            result.synthesis = await phase_synthesis(
                result, self.llm, self._build_system_prompt()
            )
        self._write_artifact(task.id, "phase3_synthesis", asdict(result.synthesis))

        # Scope the gated-out count to agents that actually analyzed:
        # a roster member excluded before Phase 1 fails the same gates
        # again here and is not a mid-run voter loss.
        analyzed_names = {a.agent_name for a in result.analyses}
        with phase_span("deliberation.phase.vote", agent_count=len(self.agents)):
            result.votes, result.vote_gated_count = await self._phase_voting(
                task, result.synthesis, analyzed_names
            )
        if result.vote_gated_count:
            # A mid-run shrunken voter set must never present as clean.
            result.degraded = True
            logger.warning(
                f"[RoundTable] Degraded result: {result.vote_gated_count} voter(s) "
                f"gated out at Phase 3 -- consensus computed over "
                f"{len(result.votes)} remaining vote(s)"
            )
        self._write_artifact(task.id, "phase3_votes", [asdict(v) for v in result.votes])

        # Collusion detection (opt-in, detect-only, fire-and-forget):
        # record this round's votes for lockstep analysis.
        if self._collusion_detector is not None and result.votes:
            from .round_table_helpers import record_collusion_votes
            await record_collusion_votes(self._collusion_detector, task, result.votes)

        result.consensus_reached = result.approval_rate >= self.config.consensus_threshold
        result.duration_seconds = (datetime.now() - start).total_seconds()

        from .round_table_helpers import apply_approval_gate
        apply_approval_gate(result, self.config, self._checkin_manager)

        self._write_artifact(task.id, "result_final", {
            "consensus": result.consensus_reached,
            "approval_rate": result.approval_rate,
            "duration": result.duration_seconds,
            "requires_approval": result.requires_approval,
            "degraded": result.degraded,
            "vote_gated_count": result.vote_gated_count,
        })

        logger.info(
            f"[RoundTable] Complete: consensus={'YES' if result.consensus_reached else 'NO'} "
            f"({result.approval_rate:.0%}), {result.duration_seconds:.1f}s"
        )
        return result

    def _build_system_prompt(self) -> str:
        """Stable system prompt (see round_table_helpers.build_system_prompt)."""
        from .round_table_helpers import build_system_prompt

        return build_system_prompt(self.agents)

    async def _phase_strategy(self, task: RoundTableTask) -> StrategyPlan:
        """Phase 0 (see round_table_helpers.phase_strategy)."""
        from .round_table_helpers import phase_strategy

        return await phase_strategy(
            task, self.llm, self._build_system_prompt(), self.agents
        )

    async def _phase_independent(
        self, task: RoundTableTask
    ) -> tuple[list[AgentAnalysis], int, AgentAnalysis | None]:
        """Phase 1: All agents analyze independently and in PARALLEL.

        Each agent passes identity and rate-limit gates before dispatch, and
        its task context is scope-filtered by its capability. Gated/failed
        agents are logged and excluded, never fatal.

        Returns (analyses, failed_agent_count, sentinel_analysis):
        failed_agent_count is agents skipped by a gate plus agents whose
        analyze() raised; sentinel_analysis is the core Sentinel OBJECT's
        analysis, captured at dispatch (identity-bound, immune to
        agent_name spoofing) and BEFORE the evidence pipeline.
        """
        from .dispatch_helpers import dispatch_with_gates

        capture = {"agent": self._sentinel_agent} if self._sentinel_enforcement else None
        rate_limiter = getattr(self._registry, "rate_limiter", None)
        analyses, skipped, failed = await dispatch_with_gates(
            self.agents, task, self._registry, rate_limiter, "RoundTable",
            baseline_tracker=self._baseline_tracker,
            store=self._learning_store,
            delegation_recorder=self._delegation_recorder,
            rate_limit_exempt=self._rate_limit_exempt,
            capture_sink=capture,
        )
        sentinel_analysis = capture.get("analysis") if capture else None

        if self.config.enforce_evidence:
            from .round_table_helpers import enforce_evidence
            analyses = await enforce_evidence(analyses, task, self.llm)

        return analyses, skipped + failed, sentinel_analysis

    async def _phase_challenge(
        self, task: RoundTableTask, analyses: list[AgentAnalysis]
    ) -> list[AgentChallenge]:
        """Phase 2 dispatch -- same gates as Phase 1, re-checked
        (see dispatch_helpers.run_challenge_phase)."""
        from .dispatch_helpers import run_challenge_phase

        return await run_challenge_phase(
            self.agents, task, analyses, self._registry,
            store=self._learning_store,
            delegation_recorder=self._delegation_recorder,
            rate_limit_exempt=self._rate_limit_exempt,
        )

    async def _phase_voting(
        self, task: RoundTableTask, synthesis: SynthesisResult,
        analyzed_names: set[str] | None = None,
    ) -> tuple[list[AgentVote], int]:
        """Phase 3b dispatch -- same gates as Phase 1, re-checked.

        Returns (votes, gated_out_count) where the count covers only
        MID-RUN losses (agents that produced a Phase 1 analysis but are
        gated out here); a non-zero count marks the result degraded
        (see dispatch_helpers.run_voting_phase)."""
        from .dispatch_helpers import run_voting_phase

        return await run_voting_phase(
            self.agents, task, synthesis, self._registry,
            midrun_names=analyzed_names,
            store=self._learning_store,
            delegation_recorder=self._delegation_recorder,
            rate_limit_exempt=self._rate_limit_exempt,
        )

    def _write_artifact(self, task_id: str, phase: str, data: Any) -> None:
        """Write intermediate results (see round_table_helpers.write_artifact)."""
        from .round_table_helpers import write_artifact

        write_artifact(self.config, task_id, phase, data)
