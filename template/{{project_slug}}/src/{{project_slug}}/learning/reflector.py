"""
Reflector -- deterministic process-level lesson extraction.

Runs after a round table session and records structured observations about
HOW the deliberation worked (agent effectiveness, evidence patterns,
challenge impact, dissent value) rather than WHAT was decided. All analysis
uses structured result fields only -- no free-text parsing, no LLM calls,
$0 ongoing cost.

This module MUST NOT import from agents/ or orchestration/ (layering).
All round-table result access is via duck typing with getattr().

Reflections are capped (per session and per tenant per day) and persisted
to the reflections table; read them back via GET /api/v1/reflections.

Keep this file under 300 lines. (Raised from 250: the four detector
functions live here by design; if another detector is added, move the
detectors to a sibling module.)
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .store import LearningStore

logger = logging.getLogger(__name__)

DAILY_CAP = 50
MAX_REFLECTIONS_PER_SESSION = 3
MIN_CITATION_COUNT = 2
MIN_EVIDENCE_TAG_COUNT = 3


class ReflectionType:
    """Bounded set of reflection types, each from one structured analysis."""

    AGENT_EFFECTIVENESS = "agent_effectiveness"
    EVIDENCE_PATTERN = "evidence_pattern"
    CHALLENGE_IMPACT = "challenge_impact"
    DISSENT_VALUE = "dissent_value"

    ALL = (AGENT_EFFECTIVENESS, EVIDENCE_PATTERN, CHALLENGE_IMPACT, DISSENT_VALUE)


@dataclass
class Reflection:
    """A process-level lesson -- mirrors the reflections table."""

    tenant_id: str = "default"
    source_task_id: str = ""
    reflection_type: str = ReflectionType.AGENT_EFFECTIVENESS
    title: str = ""
    detail: str = ""
    quality_metrics: dict[str, Any] = field(default_factory=dict)
    status: str = "recorded"
    # Acting user whose deliberation produced this reflection ("" for
    # library callers without a user identity).
    created_by: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


def _to_row(r: Reflection) -> dict:
    return {
        "id": r.id,
        "tenant_id": r.tenant_id,
        "source_task_id": r.source_task_id,
        "reflection_type": r.reflection_type,
        "title": r.title,
        "detail": r.detail,
        "quality_metrics_json": json.dumps(r.quality_metrics, default=str),
        "status": r.status,
        "created_at": r.created_at,
        "created_by": r.created_by,
    }


def _count_today(store: LearningStore, tenant_id: str) -> int:
    """Count today's reflections for a tenant (created_at date prefix)."""
    today = datetime.now().date().isoformat()
    rows = store.query(
        "reflections",
        {"tenant_id": tenant_id},
        order_by="created_at DESC",
        limit=DAILY_CAP + 1,
    )
    return sum(1 for row in rows if str(row.get("created_at", "")).startswith(today))


def reflect(
    result: Any, tenant_id: str, store: LearningStore, created_by: str = ""
) -> list[Reflection]:
    """Extract 0-3 process reflections from a round table result.

    Best-effort by design: storage failures are logged and skipped, and a
    daily per-tenant cap bounds table growth. `result` is duck-typed
    (RoundTableResult-shaped); missing fields simply produce no reflections.

    created_by attributes stored reflections to the acting user whose
    deliberation produced them (the API passes auth.user_id; the default
    "" keeps direct library callers unchanged).
    """
    try:
        today_count = _count_today(store, tenant_id)
    except Exception:
        today_count = 0
    if today_count >= DAILY_CAP:
        logger.warning(
            "[Reflector] Daily cap reached for tenant %s (%d/%d)",
            tenant_id, today_count, DAILY_CAP,
        )
        return []

    task_id = getattr(result, "task_id", "") or ""
    synthesis = getattr(result, "synthesis", None)
    analyses = getattr(result, "analyses", []) or []
    challenges = getattr(result, "challenges", []) or []
    votes = getattr(result, "votes", []) or []
    key_findings = (getattr(synthesis, "key_findings", []) or []) if synthesis else []
    minority_views = (getattr(synthesis, "minority_views", []) or []) if synthesis else []

    candidates: list[tuple[float, Reflection]] = []
    for detect_fn, args in [
        (_detect_agent_effectiveness, (key_findings, task_id, tenant_id)),
        (_detect_evidence_pattern, (analyses, task_id, tenant_id)),
        (_detect_challenge_impact, (challenges, minority_views, task_id, tenant_id)),
        (_detect_dissent_value, (votes, key_findings, task_id, tenant_id)),
    ]:
        pair = detect_fn(*args)
        if pair:
            candidates.append(pair)

    candidates.sort(key=lambda x: x[0], reverse=True)
    stored: list[Reflection] = []
    for _score, reflection in candidates[:MAX_REFLECTIONS_PER_SESSION]:
        if DAILY_CAP - today_count - len(stored) <= 0:
            break
        reflection.created_by = created_by or ""
        try:
            store.insert("reflections", _to_row(reflection))
            stored.append(reflection)
        except Exception as exc:
            logger.warning(
                "[Reflector] Store failed for %s: %s", reflection.reflection_type, exc
            )
    return stored


def _detect_agent_effectiveness(
    key_findings: list, task_id: str, tenant_id: str
) -> tuple[float, Reflection] | None:
    """Which agent contributed the most synthesis key findings?"""
    if not key_findings:
        return None
    counts: dict[str, int] = {}
    for kf in key_findings:
        name = kf.get("agent_name", "") if isinstance(kf, dict) else ""
        if name:
            counts[name] = counts.get(name, 0) + 1
    top = max(counts.items(), key=lambda x: x[1], default=("", 0))
    if top[1] < MIN_CITATION_COUNT:
        return None
    score = top[1] / max(len(key_findings), 1)
    return (
        score,
        Reflection(
            tenant_id=tenant_id,
            source_task_id=task_id,
            reflection_type=ReflectionType.AGENT_EFFECTIVENESS,
            title=f"Agent '{top[0]}' cited {top[1]} times in synthesis",
            detail=(
                f"Agent '{top[0]}' contributed {top[1]} of "
                f"{len(key_findings)} key findings."
            ),
            quality_metrics={
                "citation_count": top[1], "total_findings": len(key_findings)
            },
        ),
    )


def _detect_evidence_pattern(
    analyses: list, task_id: str, tenant_id: str
) -> tuple[float, Reflection] | None:
    """Which evidence tag dominated the independent analyses?"""
    tag_counts: dict[str, int] = {}
    total_obs = 0
    for a in analyses:
        for obs in getattr(a, "observations", []) or []:
            total_obs += 1
            ev = obs.get("evidence", "") if isinstance(obs, dict) else ""
            if ev:
                tag_counts[ev] = tag_counts.get(ev, 0) + 1
    top = max(tag_counts.items(), key=lambda x: x[1], default=("", 0))
    if top[1] < MIN_EVIDENCE_TAG_COUNT:
        return None
    score = top[1] / max(total_obs, 1)
    return (
        score,
        Reflection(
            tenant_id=tenant_id,
            source_task_id=task_id,
            reflection_type=ReflectionType.EVIDENCE_PATTERN,
            title=f"Evidence '{str(top[0])[:80]}' appeared in {top[1]} observations",
            detail=(
                f"The same evidence was cited {top[1]} times across "
                f"{len(analyses)} agent(s) ({total_obs} observations)."
            ),
            quality_metrics={"tag_frequency": top[1], "total_observations": total_obs},
        ),
    )


def _detect_challenge_impact(
    challenges: list, minority_views: list, task_id: str, tenant_id: str
) -> tuple[float, Reflection] | None:
    """Did the challenge phase move positions (concessions + minority views)?"""
    concession_count = sum(len(getattr(c, "concessions", []) or []) for c in challenges)
    minority_count = len(minority_views)
    if concession_count == 0 or minority_count == 0:
        return None
    score = min((concession_count + minority_count) / 10, 1.0)
    return (
        score,
        Reflection(
            tenant_id=tenant_id,
            source_task_id=task_id,
            reflection_type=ReflectionType.CHALLENGE_IMPACT,
            title=(
                f"Challenge phase: {concession_count} concessions, "
                f"{minority_count} minority views"
            ),
            detail=(
                f"The challenge phase produced {concession_count} concession(s) "
                f"and {minority_count} preserved minority view(s)."
            ),
            quality_metrics={
                "concession_count": concession_count,
                "minority_view_count": minority_count,
            },
        ),
    )


def _detect_dissent_value(
    votes: list, key_findings: list, task_id: str, tenant_id: str
) -> tuple[float, Reflection] | None:
    """Were dissenting voters still incorporated into the synthesis?"""
    dissent_names = {
        getattr(v, "agent_name", "")
        for v in votes
        if not getattr(v, "approve", True)
    } - {""}
    if not dissent_names:
        return None
    finding_names = set()
    for kf in key_findings:
        name = kf.get("agent_name", "") if isinstance(kf, dict) else ""
        if name:
            finding_names.add(name)
    overlap = dissent_names & finding_names
    if not overlap:
        return None
    score = len(overlap) / max(len(dissent_names), 1)
    return (
        score,
        Reflection(
            tenant_id=tenant_id,
            source_task_id=task_id,
            reflection_type=ReflectionType.DISSENT_VALUE,
            title=f"Dissenter(s) incorporated in synthesis: {', '.join(sorted(overlap))}",
            detail=(
                f"{len(overlap)} dissenting agent(s) still had findings included "
                "in the final synthesis -- dissent was not discarded."
            ),
            quality_metrics={
                "overlap_count": len(overlap), "dissent_count": len(dissent_names)
            },
        ),
    )
