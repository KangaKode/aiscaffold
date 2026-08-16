"""
Round Table API -- submit tasks for multi-agent analysis.

  POST /api/v1/round-table/tasks       -- Submit a task
  GET  /api/v1/round-table/tasks/{id}  -- Get result (poll for async)

Security:
  - Input content is size-limited (MAX_CONTENT_SIZE)
  - Results are cached with TTL and size limit (LRU eviction)
  - Agent IDs are validated as safe identifiers
"""

import asyncio
import dataclasses
import logging
import uuid
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Request

from ...llm import create_client, set_tenant_context
from ...observability.metrics import record_deliberation
from ...orchestration.deliberation_audit import audited_round_table
from ...orchestration.ingest_scan import scan_user_message
from ...orchestration.round_table import (
    RoundTable,
    RoundTableConfig,
    RoundTableTask,
)
from ...security import ValidationError, validate_length
from ..middleware.auth import AuthContext, auth_scope_key, verify_api_key
from ..middleware.rate_limit import check_rate_limit
from ..models.requests import RoundTableTaskRequest
from ..models.responses import (
    AnalysisResponse,
    ChallengeResponse,
    ClaimClosureResponse,
    IsaClosureReportResponse,
    PremiseChallengeResponse,
    RoundTableResultResponse,
    SynthesisResponse,
    VoteResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_CONTENT_SIZE = 500_000
MAX_CACHED_RESULTS = 1000

# The only RoundTableConfig fields a caller may override per task.
# Safety-relevant fields (sentinel_enforcement, enforce_evidence,
# premise_challenge_enabled, include_core_agents, ...) are deliberately
# NOT caller-tunable: they are operator policy, set in the app config.
CALLER_TUNABLE_CONFIG_FIELDS = frozenset({
    "enable_strategy_phase",
    "enable_challenge_phase",
    "consensus_threshold",
    "require_human_approval",
    "min_quorum",
})

# Bounded metric outcomes for Sentinel-enforcement refusals, keyed by
# SentinelRefusal.reason. The bare "refused" outcome stays premise-gate-only.
_SENTINEL_OUTCOMES = {
    "sentinel_high_risk": "refused_sentinel",
    "sentinel_premise": "refused_sentinel_premise",
    "sentinel_unavailable": "refused_sentinel_unavailable",
    "sentinel_missing": "refused_sentinel_missing",
}

_results_cache: OrderedDict[str, RoundTableResultResponse] = OrderedDict()


def _auth_scope(auth: AuthContext) -> str:
    """Scope user-owned round-table artifacts to the auth context."""
    return auth_scope_key(auth)


def _result_key(task_id: str, auth: AuthContext) -> str:
    return f"{_auth_scope(auth)}:{task_id}"


def _cache_result(cache_key: str, result: RoundTableResultResponse) -> None:
    """Store result with LRU eviction."""
    _results_cache[cache_key] = result
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

    All registered agents (or a subset via agent_ids) analyze the task through
    the phased protocol: Premise Gate -> Strategy -> Independent -> Challenge ->
    Synthesis. A refused premise returns status="refused" with the gate
    outcome; with opt-in Sentinel enforcement on, a Sentinel refusal
    returns status="refused" with refusal_source="sentinel".
    """
    registry = request.app.state.registry
    config: RoundTableConfig = request.app.state.round_table_config
    metrics = request.app.state.metrics

    try:
        validate_length(task_request.content, "content", min_length=1, max_length=MAX_CONTENT_SIZE)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Detect-only Layer 1 scan on the submitted task content (logs +
    # integrity flag; never blocks, never mutates). Off-loop: the flag
    # write is blocking store I/O.
    await asyncio.to_thread(
        scan_user_message,
        task_request.content,
        surface="round_table",
        store=getattr(request.app.state, "learning_store", None),
        tenant_id=auth.tenant_id,
    )

    if registry.count == 0:
        raise HTTPException(
            status_code=400,
            detail="No agents registered. Register at least one agent first.",
        )

    # Agent selection is tenant-scoped: only agents visible to the caller's
    # tenant (public, or registered by the same tenant) can participate.
    # Cross-tenant agent_ids get the same "not found" as missing ones, so
    # existence does not leak across tenants.
    visible = {
        entry.agent.name: entry.agent
        for entry in registry.list_for_tenant(auth.tenant_id)
    }
    if not visible:
        raise HTTPException(
            status_code=400,
            detail="No agents visible to your tenant. Register at least one agent first.",
        )

    if task_request.agent_ids:
        missing_agent_ids = [
            aid for aid in task_request.agent_ids if aid not in visible
        ]
        if missing_agent_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Requested agents not found: {missing_agent_ids}",
            )
        agents = [visible[aid] for aid in task_request.agent_ids]
    else:
        agents = list(visible.values())

    if task_request.config_overrides:
        # Callers may tune ONLY the allowlisted fields. dataclasses.replace
        # keeps every other field of the app config intact -- the previous
        # full rebuild silently reset non-allowlisted fields (including
        # safety settings like sentinel_enforcement and enforce_evidence)
        # to their dataclass defaults. Unknown/non-allowlisted keys in the
        # payload are ignored.
        overrides = task_request.config_overrides
        tunable = {
            key: overrides[key]
            for key in CALLER_TUNABLE_CONFIG_FIELDS
            if key in overrides
        }
        if tunable:
            config = dataclasses.replace(config, **tunable)

    task_id = uuid.uuid4().hex[:16]
    isa = None
    if getattr(task_request, "isa", None) is not None:
        from ...orchestration.task_isa import TaskClaim, TaskISA

        isa = TaskISA(
            version=task_request.isa.version,
            ideal_summary=task_request.isa.ideal_summary,
            claims=[
                TaskClaim(
                    id=c.id,
                    statement=c.statement,
                    evidence_kind=c.evidence_kind,  # type: ignore[arg-type]
                    required=c.required,
                )
                for c in task_request.isa.claims
            ],
        )
    task = RoundTableTask(
        id=task_id,
        content=task_request.content,
        context=task_request.context,
        constraints=task_request.constraints,
        # Tenant attribution for the opt-in detection hooks (baseline
        # stats, collusion flags) -- findings land in the caller's tenant.
        tenant_id=auth.tenant_id,
        isa=isa,
    )

    # Institutional knowledge (best-effort): the same approved corrections
    # and error schemas that ground /resolve are injected into the task
    # context, so deliberating agents see what the platform already learned.
    # Wrapped as untrusted content; scope filtering applies per agent.
    try:
        from ...learning.knowledge_context import build_knowledge_context
        from ...security import wrap_user_content

        corrections_manager = getattr(request.app.state, "corrections_manager", None)
        knowledge = build_knowledge_context(
            corrections_manager=corrections_manager,
            learning_store=getattr(request.app.state, "learning_store", None)
            or getattr(corrections_manager, "store", None),
            tenant_id=auth.tenant_id,
        )
        if knowledge:
            task.context = dict(task.context or {})
            task.context["institutional_knowledge"] = wrap_user_content(
                knowledge, label="INSTITUTIONAL_KNOWLEDGE"
            )
    except Exception as e:
        logger.warning(f"[RoundTableAPI] Knowledge context failed (non-fatal): {e}")

    # MCP enrichment (best-effort): when participating agents declare mcp:*
    # scopes and a matching server is registered for this tenant, fetch its
    # data into the task context. Scope filtering then limits which agents
    # see it. Failures degrade the context, never the task.
    try:
        mcp_registry = getattr(request.app.state, "mcp_registry", None)
        mcp_client = getattr(request.app.state, "mcp_client", None)
        if mcp_registry is not None and mcp_client is not None:
            from ...orchestration.mcp_enrichment import (
                collect_mcp_scopes,
                enrich_mcp_data,
            )

            # Resolve by agent OBJECT identity: the roster can include
            # public agents from other tenants, and a bare-name lookup
            # would resolve to None on a cross-tenant name collision.
            entries = [registry.entry_for_agent(a) for a in agents]
            needed = collect_mcp_scopes([e for e in entries if e is not None])
            if needed:
                task.context = await enrich_mcp_data(
                    task.context, needed, auth.tenant_id, mcp_client, mcp_registry
                )
    except Exception as e:
        logger.warning(f"[RoundTableAPI] MCP enrichment failed (non-fatal): {e}")

    # Budget/routing tenant context for every LLM call in this
    # deliberation (mirrors ChatOrchestrator.chat): without it, budget
    # checks and model-routing downgrades run against the "default"
    # tenant instead of the caller's.
    set_tenant_context(auth.tenant_id)

    try:
        llm = getattr(request.app.state, "llm_client", None) or create_client()
        rt = RoundTable(
            agents=agents,
            config=config,
            llm_client=llm,
            registry=registry,
            # Opt-in detection hooks (None unless enabled at the gateway).
            baseline_tracker=getattr(request.app.state, "baseline_tracker", None),
            collusion_detector=getattr(
                request.app.state, "collusion_detector", None
            ),
            # Store enables the agent_identity_blocked flag at the gates.
            learning_store=getattr(request.app.state, "learning_store", None),
            delegation_recorder=getattr(
                request.app.state, "delegation_recorder", None
            ),
        )
        auditor = getattr(request.app.state, "deliberation_auditor", None)
        if auditor is not None:
            result = await audited_round_table(
                rt,
                task,
                auditor,
                tenant_id=auth.tenant_id,
                correlation_id=task_id,
                user_id=auth.user_id,
            )
        else:
            result = await rt.run(task)

        try:
            indexer = getattr(request.app.state, "transcript_indexer", None)
            if indexer:
                indexer.index_result(
                    result,
                    task_content=task_request.content,
                    owner_key=_auth_scope(auth),
                )
        except Exception as e:
            logger.warning(f"[RoundTableAPI] Transcript indexing failed: {e}")

        # Process reflections (best-effort): deterministic lessons about HOW
        # the deliberation worked, readable via GET /api/v1/reflections.
        try:
            store = getattr(request.app.state, "learning_store", None)
            if store is not None:
                from ...learning.reflector import reflect

                reflect(
                    result, tenant_id=auth.tenant_id, store=store,
                    created_by=auth.user_id,
                )
        except Exception as e:
            logger.warning(f"[RoundTableAPI] Reflection extraction failed: {e}")

        metrics["tasks_completed"] += 1
        metrics["total_duration"] += result.duration_seconds
        metrics["total_agent_calls"] += len(agents) * 3

        pcr = result.premise_challenge
        sentinel_refusal = getattr(result, "sentinel_refusal", None)
        canary_refusal = getattr(result, "canary_refusal", None)
        if sentinel_refusal is not None:
            refusal_source = "sentinel"
            refusal_reason = sentinel_refusal.reason
            outcome = _SENTINEL_OUTCOMES.get(
                sentinel_refusal.reason, "refused_sentinel"
            )
        elif canary_refusal is not None:
            refusal_source = "canary"
            refusal_reason = canary_refusal.reason
            outcome = "refused_canary"
        elif pcr is not None:
            refusal_source = "premise_gate"
            refusal_reason = "premise_challenged"
            outcome = "refused"
        else:
            refusal_source = None
            refusal_reason = None
            outcome = "completed"
        record_deliberation(outcome, result.duration_seconds)
        response = RoundTableResultResponse(
            task_id=task_id,
            status="refused" if refusal_source is not None else "completed",
            refusal_source=refusal_source,
            refusal_reason=refusal_reason,
            premise_challenge=PremiseChallengeResponse(
                what_is_wrong=pcr.what_is_wrong,
                what_is_missing=pcr.what_is_missing,
                better_question=pcr.better_question,
                refusing_agents=[r["agent_name"] for r in pcr.challenge_reasons],
                agents_who_would_proceed=pcr.agents_who_would_proceed,
            )
            if pcr is not None
            else None,
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
            degraded=result.degraded,
            failed_agent_count=result.failed_agent_count,
            vote_gated_count=result.vote_gated_count,
            isa_closure=(
                IsaClosureReportResponse(
                    claim_closures=[
                        ClaimClosureResponse(
                            claim_id=c.claim_id,
                            status=c.status,
                            detail=c.detail,
                        )
                        for c in result.isa_closure.claim_closures
                    ],
                    all_required_closed=result.isa_closure.all_required_closed,
                    error=result.isa_closure.error,
                )
                if getattr(result, "isa_closure", None) is not None
                else None
            ),
        )

        _cache_result(_result_key(task_id, auth), response)
        return response

    except Exception as e:
        metrics["tasks_failed"] += 1
        record_deliberation("failed", 0.0)
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
    cache_key = _result_key(task_id, auth)
    if cache_key not in _results_cache:
        raise HTTPException(
            status_code=404, detail=f"Task '{task_id}' not found"
        )
    _results_cache.move_to_end(cache_key)
    return _results_cache[cache_key]


@router.get("/round-table/search")
async def search_transcripts(
    q: str,
    request: Request,
    limit: int = 10,
    consensus_only: bool = False,
    auth: AuthContext = Depends(verify_api_key),
    _rate: None = Depends(check_rate_limit),
) -> dict:
    """Semantic search over past round table deliberations."""
    try:
        validate_length(q, "query", min_length=1, max_length=1000)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    indexer = getattr(request.app.state, "transcript_indexer", None)
    if indexer is None:
        raise HTTPException(
            status_code=503,
            detail="Transcript search not available (indexer not initialized)",
        )

    results = indexer.search(
        query=q,
        limit=min(limit, 50),
        consensus_only=consensus_only,
        owner_key=_auth_scope(auth),
    )
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
