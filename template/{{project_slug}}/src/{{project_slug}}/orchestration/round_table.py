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

import asyncio
import logging
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

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
    """Input to a round table session."""

    id: str
    content: str
    context: dict[str, Any] = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)


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
    """

    task_id: str
    premise_challenge: Any = None  # PremiseChallengeResult when the gate tripped
    strategy: StrategyPlan | None = None
    analyses: list[AgentAnalysis] = field(default_factory=list)
    challenges: list[AgentChallenge] = field(default_factory=list)
    synthesis: SynthesisResult | None = None
    votes: list[AgentVote] = field(default_factory=list)
    consensus_reached: bool = False
    duration_seconds: float = 0.0
    degraded: bool = False
    failed_agent_count: int = 0
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
    ):
        self.config = config
        self.llm = llm_client
        self._registry = registry  # Optional: enables identity/rate-limit gates
        self._checkin_manager = checkin_manager  # Optional: approval check-ins
        self._core_agent_names: set[str] = set()

        if config.include_core_agents:
            try:
                from ..agents.core import get_core_agents
                core = get_core_agents(llm_client=llm_client)
                core_names = {a.name for a in core}
                self._core_agent_names = core_names
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
            result.strategy = await self._phase_strategy(task)
            self._write_artifact(task.id, "phase0_strategy", asdict(result.strategy))

        # Phase 1: Independent Analysis (PARALLEL -- separate context windows)
        # Wire strategy focus areas into the task context so agents specialize
        if result.strategy and result.strategy.agent_focus_areas:
            task.context["agent_focus_areas"] = result.strategy.agent_focus_areas

        logger.info(f"[RoundTable] Phase 1: Independent analysis ({len(self.agents)} agents)")
        result.analyses, result.failed_agent_count = await self._phase_independent(task)
        self._write_artifact(task.id, "phase1_analyses", [asdict(a) for a in result.analyses])

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
            result.challenges = await self._phase_challenge(task, result.analyses)
            self._write_artifact(task.id, "phase2_challenges", [asdict(c) for c in result.challenges])

        # Phase 3: Synthesis + Voting
        logger.info("[RoundTable] Phase 3: Synthesis + voting")
        from .round_table_helpers import phase_synthesis
        result.synthesis = await phase_synthesis(
            result, self.llm, self._build_system_prompt()
        )
        self._write_artifact(task.id, "phase3_synthesis", asdict(result.synthesis))

        result.votes = await self._phase_voting(task, result.synthesis)
        self._write_artifact(task.id, "phase3_votes", [asdict(v) for v in result.votes])

        result.consensus_reached = result.approval_rate >= self.config.consensus_threshold
        result.duration_seconds = (datetime.now() - start).total_seconds()

        from .round_table_helpers import apply_approval_gate
        apply_approval_gate(result, self.config, self._checkin_manager)

        self._write_artifact(task.id, "result_final", {
            "consensus": result.consensus_reached,
            "approval_rate": result.approval_rate,
            "duration": result.duration_seconds,
            "requires_approval": result.requires_approval,
        })

        logger.info(
            f"[RoundTable] Complete: consensus={'YES' if result.consensus_reached else 'NO'} "
            f"({result.approval_rate:.0%}), {result.duration_seconds:.1f}s"
        )
        return result

    def _build_system_prompt(self) -> str:
        """Build the stable system prompt (cached across calls)."""
        agent_info = ", ".join(f"{a.name} ({a.domain})" for a in self.agents)
        return (
            f"You are an orchestrator coordinating {len(self.agents)} specialist agents: "
            f"{agent_info}.\n\n"
            f"Rules:\n"
            f"- Preserve ALL evidence fields from agent outputs\n"
            f"- Do NOT summarize away supporting quotes, data, or citations\n"
            f"- Surface disagreements -- minority views are valuable\n"
            f"- Return valid JSON"
        )

    async def _phase_strategy(self, task: RoundTableTask) -> StrategyPlan:
        """Phase 0: Orchestrator plans before dispatching."""
        from ..llm import CacheablePrompt

        prompt = CacheablePrompt(
            system=self._build_system_prompt(),
            user_message=(
                f"Task: {task.content}\n\n"
                f"Before dispatching the team, plan your strategy:\n"
                f"1. How does this task decompose into sub-problems?\n"
                f"2. What should each agent specifically focus on?\n"
                f"3. What disagreements do you anticipate between agents?\n"
                f"4. What are the success criteria?\n\n"
                'Return JSON: {"task_decomposition": [...], "agent_focus_areas": {...}, '
                '"anticipated_tensions": [...], "success_criteria": [...]}'
            ),
        )
        try:
            from ..llm.json_parser import extract_json

            response = await self.llm.call(prompt=prompt, role="synthesis", temperature=0.3)
            data = extract_json(response.content)
            if data is None:
                logger.warning("[RoundTable] Strategy phase returned unparseable JSON")
                return StrategyPlan(reasoning=response.content)
            return StrategyPlan(
                task_decomposition=data.get("task_decomposition", []),
                agent_focus_areas=data.get("agent_focus_areas", {}),
                anticipated_tensions=data.get("anticipated_tensions", []),
                success_criteria=data.get("success_criteria", []),
                reasoning=response.content,
            )
        except Exception as e:
            logger.warning(f"[RoundTable] Strategy phase failed: {e}")
            return StrategyPlan(
                task_decomposition=["Full analysis"],
                agent_focus_areas={a.name: a.domain for a in self.agents},
                success_criteria=["Actionable recommendations with evidence"],
            )

    async def _phase_independent(
        self, task: RoundTableTask
    ) -> tuple[list[AgentAnalysis], int]:
        """Phase 1: All agents analyze independently and in PARALLEL.

        Each agent passes identity and rate-limit gates before dispatch, and
        its task context is scope-filtered by its capability. Gated/failed
        agents are logged and excluded, never fatal.

        Returns (analyses, failed_agent_count) where failed_agent_count is
        agents skipped by a gate plus agents whose analyze() raised.
        """
        from .dispatch_helpers import dispatch_with_gates

        rate_limiter = getattr(self._registry, "rate_limiter", None)
        analyses, skipped, failed = await dispatch_with_gates(
            self.agents, task, self._registry, rate_limiter, "RoundTable"
        )

        if self.config.enforce_evidence:
            from .round_table_helpers import enforce_evidence
            analyses = await enforce_evidence(analyses, task, self.llm)

        return analyses, skipped + failed

    def _gate_agents(self, task: RoundTableTask) -> list[tuple[Any, RoundTableTask]]:
        """Run the shared dispatch gates (identity/suspension, rate limit,
        scope filtering) for one phase, exactly as Phase 1 does.

        Gates are re-checked per phase, so an agent suspended mid-run is
        excluded from every later phase.
        """
        from .dispatch_helpers import gate_agents

        rate_limiter = getattr(self._registry, "rate_limiter", None)
        gated, skipped = gate_agents(
            self.agents, task, self._registry, rate_limiter, "RoundTable"
        )
        if skipped:
            logger.warning(
                f"[RoundTable] {skipped} agent(s) blocked by dispatch gates this phase"
            )
        return [(agent, agent_task) for agent, agent_task, _ in gated]

    async def _phase_challenge(
        self, task: RoundTableTask, analyses: list[AgentAnalysis]
    ) -> list[AgentChallenge]:
        """Phase 2: Agents challenge each other (mediated hub-and-spoke).

        Dispatch passes the same gates as Phase 1 (identity/suspension,
        rate limit, scope-filtered task).
        """
        gated = self._gate_agents(task)
        results = await asyncio.gather(
            *[agent.challenge(agent_task, analyses) for agent, agent_task in gated],
            return_exceptions=True,
        )
        challenges = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error(f"[RoundTable] {gated[i][0].name} challenge failed: {r}")
                continue
            challenges.append(r)
        return challenges

    async def _phase_voting(
        self, task: RoundTableTask, synthesis: SynthesisResult
    ) -> list[AgentVote]:
        """Phase 3b: Agents vote on synthesis. Dissent is valuable.

        Dispatch passes the same gates as Phase 1 (identity/suspension,
        rate limit, scope-filtered task). Gated agents cast no vote, so
        consensus is computed over the votes actually returned -- the
        approval rate never divides by the original agent count.
        """
        gated = self._gate_agents(task)
        results = await asyncio.gather(
            *[agent.vote(agent_task, synthesis) for agent, agent_task in gated],
            return_exceptions=True,
        )
        votes = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error(f"[RoundTable] {gated[i][0].name} vote failed: {r}")
                votes.append(AgentVote(agent_name=gated[i][0].name, dissent_reason=str(r)))
                continue
            votes.append(r)
        return votes

    def _write_artifact(self, task_id: str, phase: str, data: Any) -> None:
        """Write intermediate results to filesystem for auditability."""
        if not self.config.write_artifacts:
            return
        artifact_dir = self.config.artifacts_dir / task_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / f"{phase}.json"
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
            logger.debug(f"[RoundTable] Artifact: {path}")
        except Exception as e:
            logger.warning(f"[RoundTable] Artifact write failed: {e}")
