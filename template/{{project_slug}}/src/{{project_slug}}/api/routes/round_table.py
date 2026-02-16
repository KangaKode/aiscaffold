"""
Round Table API -- submit tasks for multi-agent analysis.

  POST /api/v1/round-table/tasks       -- Submit a task
  GET  /api/v1/round-table/tasks/{id}  -- Get result (poll for async)

Security:
  - Input content is size-limited (MAX_CONTENT_SIZE)
  - Results are cached with TTL and size limit (LRU eviction)
  - Agent IDs are validated as safe identifiers
"""

import logging
import uuid
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Request

from ...llm import create_client
from ...orchestration.round_table import (
    RoundTable,
    RoundTableConfig,
    RoundTableTask,
)
from ...security import ValidationError, validate_length
from ..middleware.auth import AuthContext, verify_api_key
from ..middleware.rate_limit import check_rate_limit
from ..models.requests import RoundTableTaskRequest
from ..models.responses import (
    AnalysisResponse,
    ChallengeResponse,
    RoundTableResultResponse,
    SynthesisResponse,
    VoteResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_CONTENT_SIZE = 500_000
MAX_CACHED_RESULTS = 1000

_results_cache: OrderedDict[str, RoundTableResultResponse] = OrderedDict()


def _cache_result(task_id: str, result: RoundTableResultResponse) -> None:
    """Store result with LRU eviction."""
    _results_cache[task_id] = result
    while len(_results_cache) > MAX_CACHED_RESULTS:
        _results_cache.popitem(last=False)


@router.post("/round-table/tasks", response_model=RoundTableResultResponse)
async def submit_task(
    task_request: RoundTableTaskRequest,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> RoundTableResultResponse:
    """
    Submit a task to the round table for multi-agent analysis.

    All registered agents (or a subset via agent_ids) will analyze the task
    through the 4-phase protocol: Strategy -> Independent -> Challenge -> Synthesis.
    """
    registry = request.app.state.registry
    config: RoundTableConfig = request.app.state.round_table_config
    metrics = request.app.state.metrics

    try:
        validate_length(task_request.content, "content", min_length=1, max_length=MAX_CONTENT_SIZE)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if registry.count == 0:
        raise HTTPException(
            status_code=400,
            detail="No agents registered. Register at least one agent first.",
        )

    if task_request.agent_ids:
        agents = [
            registry.get(aid)
            for aid in task_request.agent_ids
            if registry.get(aid) is not None
        ]
        if not agents:
            raise HTTPException(
                status_code=400,
                detail=f"None of the requested agents found: {task_request.agent_ids}",
            )
    else:
        agents = registry.get_all()

    if task_request.config_overrides:
        overrides = task_request.config_overrides
        config = RoundTableConfig(
            enable_strategy_phase=overrides.get(
                "enable_strategy_phase", config.enable_strategy_phase
            ),
            enable_challenge_phase=overrides.get(
                "enable_challenge_phase", config.enable_challenge_phase
            ),
            consensus_threshold=overrides.get(
                "consensus_threshold", config.consensus_threshold
            ),
            require_human_approval=overrides.get(
                "require_human_approval", config.require_human_approval
            ),
        )

    task_id = uuid.uuid4().hex[:16]
    task = RoundTableTask(
        id=task_id,
        content=task_request.content,
        context=task_request.context,
        constraints=task_request.constraints,
    )

    try:
        llm = getattr(request.app.state, "llm_client", None) or create_client()
        rt = RoundTable(agents=agents, config=config, llm_client=llm)
        result = await rt.run(task)

        try:
            indexer = getattr(request.app.state, "transcript_indexer", None)
            if indexer:
                indexer.index_result(result, task_content=task_request.content)
        except Exception as e:
            logger.warning(f"[RoundTableAPI] Transcript indexing failed: {e}")

        metrics["tasks_completed"] += 1
        metrics["total_duration"] += result.duration_seconds
        metrics["total_agent_calls"] += len(agents) * 3

        response = RoundTableResultResponse(
            task_id=task_id,
            status="completed",
            consensus_reached=result.consensus_reached,
            approval_rate=result.approval_rate,
            analyses=[
                AnalysisResponse(
                    agent_name=a.agent_name,
                    domain=a.domain,
                    observations=a.observations,
                    recommendations=a.recommendations,
                    confidence=a.confidence,
                )
                for a in result.analyses
            ],
            challenges=[
                ChallengeResponse(
                    agent_name=c.agent_name,
                    challenges=c.challenges,
                    concessions=c.concessions,
                )
                for c in result.challenges
            ],
            synthesis=SynthesisResponse(
                recommended_direction=result.synthesis.recommended_direction,
                key_findings=result.synthesis.key_findings,
                trade_offs=result.synthesis.trade_offs,
                minority_views=result.synthesis.minority_views,
            )
            if result.synthesis
            else None,
            votes=[
                VoteResponse(
                    agent_name=v.agent_name,
                    approve=v.approve,
                    conditions=v.conditions,
                    dissent_reason=v.dissent_reason,
                )
                for v in result.votes
            ],
            duration_seconds=result.duration_seconds,
        )

        _cache_result(task_id, response)
        return response

    except Exception as e:
        metrics["tasks_failed"] += 1
        logger.error(f"[RoundTableAPI] Task {task_id} failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Internal error processing task {task_id}. Check server logs.",
        )


@router.get("/round-table/tasks/{task_id}", response_model=RoundTableResultResponse)
async def get_task_result(
    task_id: str,
    auth: AuthContext = Depends(verify_api_key),
) -> RoundTableResultResponse:
    """Get a previously completed task result."""
    if task_id not in _results_cache:
        raise HTTPException(
            status_code=404, detail=f"Task '{task_id}' not found"
        )
    return _results_cache[task_id]


@router.get("/round-table/search")
async def search_transcripts(
    q: str,
    request: Request,
    limit: int = 10,
    consensus_only: bool = False,
    auth: AuthContext = Depends(verify_api_key),
) -> dict:
    """Semantic search over past round table deliberations."""
    indexer = getattr(request.app.state, "transcript_indexer", None)
    if indexer is None:
        raise HTTPException(
            status_code=503,
            detail="Transcript search not available (indexer not initialized)",
        )

    results = indexer.search(query=q, limit=min(limit, 50), consensus_only=consensus_only)
    return {
        "query": q,
        "results": [
            {
                "task_id": r.metadata.get("task_id", ""),
                "content": r.content[:500],
                "score": r.score,
                "consensus_reached": r.metadata.get("consensus_reached", ""),
                "approval_rate": r.metadata.get("approval_rate", ""),
                "agent_names": r.metadata.get("agent_names", ""),
                "timestamp": r.metadata.get("timestamp", ""),
            }
            for r in results.results
        ],
        "total": results.total,
    }
