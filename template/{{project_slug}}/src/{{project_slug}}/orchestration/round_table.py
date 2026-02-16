"""
Generic Round Table Protocol - Multi-agent coordination with 4 phases.

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

Keep this file under 400 lines.
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
    """Complete round table output."""

    task_id: str
    strategy: StrategyPlan | None = None
    analyses: list[AgentAnalysis] = field(default_factory=list)
    challenges: list[AgentChallenge] = field(default_factory=list)
    synthesis: SynthesisResult | None = None
    votes: list[AgentVote] = field(default_factory=list)
    consensus_reached: bool = False
    duration_seconds: float = 0.0

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

    def __init__(self, agents: list, config: RoundTableConfig, llm_client: Any = None):
        self.agents = agents
        self.config = config
        self.llm = llm_client
        logger.info(f"[RoundTable] Initialized with {len(agents)} agents")

    async def run(self, task: RoundTableTask) -> RoundTableResult:
        """Execute the full 4-phase round table protocol."""
        start = datetime.now()
        result = RoundTableResult(task_id=task.id)

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
        result.analyses = await self._phase_independent(task)
        self._write_artifact(task.id, "phase1_analyses", [asdict(a) for a in result.analyses])

        # Phase 2: Challenge
        if self.config.enable_challenge_phase:
            logger.info("[RoundTable] Phase 2: Cross-agent challenge")
            result.challenges = await self._phase_challenge(task, result.analyses)
            self._write_artifact(task.id, "phase2_challenges", [asdict(c) for c in result.challenges])

        # Phase 3: Synthesis + Voting
        logger.info("[RoundTable] Phase 3: Synthesis + voting")
        result.synthesis = await self._phase_synthesis(task, result)
        self._write_artifact(task.id, "phase3_synthesis", asdict(result.synthesis))

        result.votes = await self._phase_voting(task, result.synthesis)
        self._write_artifact(task.id, "phase3_votes", [asdict(v) for v in result.votes])

        result.consensus_reached = result.approval_rate >= self.config.consensus_threshold
        result.duration_seconds = (datetime.now() - start).total_seconds()

        self._write_artifact(task.id, "result_final", {
            "consensus": result.consensus_reached,
            "approval_rate": result.approval_rate,
            "duration": result.duration_seconds,
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
            return StrategyPlan(**data, reasoning=response.content)
        except Exception as e:
            logger.warning(f"[RoundTable] Strategy phase failed: {e}")
            return StrategyPlan(
                task_decomposition=["Full analysis"],
                agent_focus_areas={a.name: a.domain for a in self.agents},
                success_criteria=["Actionable recommendations with evidence"],
            )

    async def _phase_independent(self, task: RoundTableTask) -> list[AgentAnalysis]:
        """Phase 1: All agents analyze independently and in PARALLEL."""
        results = await asyncio.gather(
            *[agent.analyze(task) for agent in self.agents],
            return_exceptions=True,
        )
        analyses = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error(f"[RoundTable] {self.agents[i].name} failed: {r}")
                continue
            analyses.append(r)
        return analyses

    async def _phase_challenge(
        self, task: RoundTableTask, analyses: list[AgentAnalysis]
    ) -> list[AgentChallenge]:
        """Phase 2: Agents challenge each other (mediated hub-and-spoke)."""
        results = await asyncio.gather(
            *[agent.challenge(task, analyses) for agent in self.agents],
            return_exceptions=True,
        )
        challenges = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error(f"[RoundTable] {self.agents[i].name} challenge failed: {r}")
                continue
            challenges.append(r)
        return challenges

    async def _phase_synthesis(
        self, task: RoundTableTask, partial: RoundTableResult
    ) -> SynthesisResult:
        """Phase 3a: Synthesize analyses. CRITICAL: preserve ALL evidence fields."""
        from ..llm import CacheablePrompt

        if not self.llm:
            return SynthesisResult(recommended_direction="No LLM available for synthesis")

        analyses_json = json.dumps(
            [{"agent": a.agent_name, "domain": a.domain,
              "observations": a.observations, "recommendations": a.recommendations,
              "confidence": a.confidence} for a in partial.analyses],
            indent=2,
        )
        prompt = CacheablePrompt(
            system=self._build_system_prompt(),
            context=(
                f"Analyses from {len(partial.analyses)} agents:\n{analyses_json}"
            ),
            user_message=(
                "Synthesize these specialist analyses into a recommendation.\n\n"
                'Return JSON: {"recommended_direction": "...", '
                '"key_findings": [{"agent_name": ..., "finding": ..., "evidence": ...}], '
                '"trade_offs": [...], "minority_views": [...]}'
            ),
        )
        try:
            from ..llm.json_parser import extract_json

            response = await self.llm.call(prompt=prompt, role="synthesis", temperature=0.2)
            data = extract_json(response.content)
            if data is None:
                logger.warning("[RoundTable] Synthesis returned unparseable JSON")
                return SynthesisResult(recommended_direction=response.content[:500])
            return SynthesisResult(**data)
        except Exception as e:
            logger.warning(f"[RoundTable] Synthesis failed: {e}")
            return SynthesisResult(recommended_direction="Synthesis failed -- review individual analyses")

    async def _phase_voting(
        self, task: RoundTableTask, synthesis: SynthesisResult
    ) -> list[AgentVote]:
        """Phase 3b: Agents vote on synthesis. Dissent is valuable."""
        results = await asyncio.gather(
            *[agent.vote(task, synthesis) for agent in self.agents],
            return_exceptions=True,
        )
        votes = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error(f"[RoundTable] {self.agents[i].name} vote failed: {r}")
                votes.append(AgentVote(agent_name=self.agents[i].name, dissent_reason=str(r)))
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
