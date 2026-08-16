"""
Task ISA (Ideal State Artifact) -- optional per-task definition of done.

Phase 1: detect/report only. Claims close on tool/citation/artifact/human_ack
evidence; never invent closed; never mutate analyses or consensus math.

Keep under 380 lines.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

MAX_CLAIMS = 32
MAX_STATEMENT_LEN = 500
MAX_SUMMARY_LEN = 500
MAX_CLAIM_ID_LEN = 64

EvidenceKind = Literal["tool_result", "citation", "artifact", "human_ack"]
ClaimStatus = Literal["closed", "open", "unverifiable"]

_EVIDENCE_TAG = re.compile(
    r"\[(VERIFIED|CORROBORATED|INDICATED):\s*[^\]]+\]", re.IGNORECASE
)
# Tool evidence must be structured or an evidence tag naming a tool/mcp source —
# never the bare prose substring "tool_result".
_TOOL_EVIDENCE_TAG = re.compile(
    r"\[(VERIFIED|CORROBORATED|INDICATED):\s*(?:tool(?:_result)?|mcp)(?::[^\]]*)?\]",
    re.IGNORECASE,
)


class TaskISAValidationError(ValueError):
    """Malformed or oversized Task ISA (API should map to 4xx)."""


@dataclass
class TaskClaim:
    """One testable Ideal State claim for a task."""

    id: str
    statement: str
    evidence_kind: EvidenceKind = "citation"
    required: bool = True


@dataclass
class TaskISA:
    """Client-owned Ideal State for one task (not personal TELOS)."""

    ideal_summary: str = ""
    claims: list[TaskClaim] = field(default_factory=list)
    version: str = "1"


@dataclass
class ClaimClosure:
    """Detect-only status for one claim after evidence inspection."""

    claim_id: str
    status: ClaimStatus
    detail: str = ""


@dataclass
class IsaClosureReport:
    """Detect-only closure report; never mutates analyses."""

    claim_closures: list[ClaimClosure] = field(default_factory=list)
    all_required_closed: bool = False
    error: str | None = None


def task_isa_to_dict(isa: TaskISA) -> dict[str, Any]:
    """Serialize TaskISA for phase artifacts."""
    return asdict(isa)


def isa_closure_to_dict(report: IsaClosureReport) -> dict[str, Any]:
    """Serialize IsaClosureReport for phase artifacts / API."""
    return asdict(report)


def validate_task_isa(isa: TaskISA) -> TaskISA:
    """Validate caps and enums; raise TaskISAValidationError on failure."""
    if isa.version != "1":
        raise TaskISAValidationError("isa.version must be '1'")
    if len(isa.ideal_summary) > MAX_SUMMARY_LEN:
        raise TaskISAValidationError(
            f"isa.ideal_summary exceeds {MAX_SUMMARY_LEN} characters"
        )
    if len(isa.claims) > MAX_CLAIMS:
        raise TaskISAValidationError(f"isa.claims exceeds max of {MAX_CLAIMS}")
    seen: set[str] = set()
    allowed: set[str] = {"tool_result", "citation", "artifact", "human_ack"}
    for c in isa.claims:
        if not c.id or len(c.id) > MAX_CLAIM_ID_LEN:
            raise TaskISAValidationError(
                f"claim id missing or longer than {MAX_CLAIM_ID_LEN}"
            )
        if c.id in seen:
            raise TaskISAValidationError(f"duplicate claim id: {c.id}")
        seen.add(c.id)
        if not c.statement or len(c.statement) > MAX_STATEMENT_LEN:
            raise TaskISAValidationError(
                f"claim {c.id!r} statement missing or longer than {MAX_STATEMENT_LEN}"
            )
        if c.evidence_kind not in allowed:
            raise TaskISAValidationError(
                f"claim {c.id!r} evidence_kind must be one of {sorted(allowed)}"
            )
    return isa


def parse_task_isa(raw: Any) -> TaskISA | None:
    """Parse dict/TaskISA into validated TaskISA; None stays None."""
    if raw is None:
        return None
    if isinstance(raw, TaskISA):
        return validate_task_isa(raw)
    if not isinstance(raw, dict):
        raise TaskISAValidationError("isa must be an object")
    claims_raw = raw.get("claims", [])
    if not isinstance(claims_raw, list):
        raise TaskISAValidationError("isa.claims must be a list")
    claims: list[TaskClaim] = []
    for item in claims_raw:
        if not isinstance(item, dict):
            raise TaskISAValidationError("each claim must be an object")
        claims.append(
            TaskClaim(
                id=str(item.get("id", "")),
                statement=str(item.get("statement", "")),
                evidence_kind=item.get("evidence_kind", "citation"),  # type: ignore[arg-type]
                required=bool(item.get("required", True)),
            )
        )
    return validate_task_isa(
        TaskISA(
            version=str(raw.get("version", "1")),
            ideal_summary=str(raw.get("ideal_summary", "")),
            claims=claims,
        )
    )


def _obs_match(claim: TaskClaim, finding: str, evidence: str) -> bool:
    stmt = claim.statement.strip().lower()
    if not stmt:
        return False
    combined = f"{finding}\n{evidence}"
    lower = combined.lower()
    if stmt in lower:
        return True
    tokens = [t for t in re.split(r"\W+", stmt) if len(t) >= 4]
    if not tokens:
        return False
    return sum(1 for t in tokens if t in lower) >= max(1, (len(tokens) + 1) // 2)


# Orchestrator-owned artifact basenames / prefixes — never satisfy client artifact claims.
_RESERVED_ARTIFACT_EXACT = frozenset(
    {
        "isa",
        "isa_closure",
        "result_final",
        "phase0_strategy",
        "phase1_analyses",
        "phase1_sentinel_refusal",
        "phase2_challenges",
        "phase3_synthesis",
        "phase3_votes",
        "phase3_canary_refusal",
    }
)
_RESERVED_ARTIFACT_PREFIXES = (
    "phase0_",
    "phase1_",
    "phase2_",
    "phase3_",
)


def _is_reserved_artifact_name(name: str) -> bool:
    """True for round-table phase / ISA bookkeeping files (not client evidence)."""
    from pathlib import PurePosixPath

    stem = PurePosixPath(name).stem.lower()
    if stem in _RESERVED_ARTIFACT_EXACT:
        return True
    return any(stem.startswith(p) for p in _RESERVED_ARTIFACT_PREFIXES)


def _artifact_matches(claim: TaskClaim, name: str) -> bool:
    """Close artifact claims only on whole-token / exact matches — never short-id substrings.

    Stock deliberation artifacts (phase*_*.json, isa.json, result_final.json) never
    close a claim via id or statement match — clients must supply their own files.
    """
    from pathlib import PurePosixPath

    if _is_reserved_artifact_name(name):
        return False

    n = name.lower()
    stem = PurePosixPath(name).stem.lower()
    cid = claim.id.strip().lower()
    if cid:
        if stem == cid or n == f"{cid}.json":
            return True
        # Separator-bounded token only (c1_report.json / report_c1.json).
        if re.search(rf"(^|[_\-.]){re.escape(cid)}([_\-.]|$)", n):
            return True
    needle = claim.statement.strip().lower()
    if needle and len(needle) >= 8:
        if n == needle or stem == PurePosixPath(needle).stem.lower():
            return True
        if needle in n:
            return True
    return False


def _has_structured_tool_result(obs: dict[str, Any]) -> bool:
    raw = obs.get("tool_result")
    if raw is None:
        return False
    if isinstance(raw, str):
        return bool(raw.strip())
    if isinstance(raw, (dict, list)):
        return bool(raw)
    return True


def _claim_status(
    claim: TaskClaim,
    analyses: list[Any],
    *,
    artifact_names: list[str],
    human_ack_ids: set[str],
) -> ClaimClosure:
    try:
        if claim.evidence_kind == "human_ack":
            if claim.id in human_ack_ids:
                return ClaimClosure(claim.id, "closed", "human_ack present")
            return ClaimClosure(claim.id, "open", "human_ack missing")

        if claim.evidence_kind == "artifact":
            for name in artifact_names:
                if _artifact_matches(claim, name):
                    return ClaimClosure(claim.id, "closed", f"artifact {name}")
            return ClaimClosure(claim.id, "open", "no matching artifact")

        for analysis in analyses:
            for obs in getattr(analysis, "observations", None) or []:
                if not isinstance(obs, dict):
                    continue
                finding = str(obs.get("finding", ""))
                evidence = str(obs.get("evidence", ""))
                if not _obs_match(claim, finding, evidence):
                    continue
                combined = f"{finding}\n{evidence}"
                if claim.evidence_kind == "citation":
                    if _EVIDENCE_TAG.search(combined):
                        return ClaimClosure(
                            claim.id, "closed", "evidence-tagged finding"
                        )
                elif claim.evidence_kind == "tool_result":
                    if _has_structured_tool_result(obs) or _TOOL_EVIDENCE_TAG.search(
                        combined
                    ):
                        return ClaimClosure(claim.id, "closed", "tool_result evidence")
        return ClaimClosure(claim.id, "open", "no matching evidence")
    except Exception as exc:  # noqa: BLE001 -- never invent closed
        return ClaimClosure(
            claim.id,
            "unverifiable",
            f"{type(exc).__name__}: {exc}"[:200],
        )


def evaluate_isa_closure(
    isa: TaskISA | None,
    analyses: list[Any],
    *,
    artifact_names: list[str] | None = None,
    human_ack_ids: set[str] | None = None,
) -> IsaClosureReport | None:
    """Build detect-only closure report; None when no ISA."""
    if isa is None:
        return None
    try:
        closures = [
            _claim_status(
                c,
                analyses,
                artifact_names=list(artifact_names or []),
                human_ack_ids=set(human_ack_ids or ()),
            )
            for c in isa.claims
        ]
        required_ok = all(
            cl.status == "closed"
            for c, cl in zip(isa.claims, closures, strict=True)
            if c.required
        )
        return IsaClosureReport(
            claim_closures=closures,
            all_required_closed=required_ok,
        )
    except Exception as exc:  # noqa: BLE001
        return IsaClosureReport(
            claim_closures=[
                ClaimClosure(c.id, "unverifiable", "evaluator failure")
                for c in isa.claims
            ],
            all_required_closed=False,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )


def _human_ack_ids(context: dict[str, Any] | None) -> set[str]:
    raw = (context or {}).get("human_ack_claim_ids") or []
    return {str(x) for x in raw} if isinstance(raw, list) else set()


def _artifact_names(artifacts_dir: Path | None, task_id: str) -> list[str]:
    if not artifacts_dir:
        return []
    art_dir = Path(artifacts_dir) / task_id
    if not art_dir.is_dir():
        return []
    return [p.name for p in art_dir.iterdir()]


def record_isa_closure(
    *,
    isa: TaskISA | None,
    analyses: list[Any],
    task_id: str,
    context: dict[str, Any] | None,
    artifacts_dir: Path | None,
    write_artifact: Callable[[str, str, Any], None],
) -> IsaClosureReport | None:
    """Evaluate + persist ISA artifacts; None when no ISA (detect-only)."""
    if isa is None:
        return None
    report = evaluate_isa_closure(
        isa,
        analyses,
        artifact_names=_artifact_names(artifacts_dir, task_id),
        human_ack_ids=_human_ack_ids(context),
    )
    write_artifact(task_id, "isa", task_isa_to_dict(isa))
    if report is not None:
        write_artifact(task_id, "isa_closure", isa_closure_to_dict(report))
    return report


def merge_isa_into_final_payload(
    payload: dict[str, Any], report: IsaClosureReport | None
) -> None:
    """Add isa_closure fields to result_final when a report exists."""
    if report is None:
        return
    payload["isa_closure"] = isa_closure_to_dict(report)
    payload["all_required_closed"] = report.all_required_closed
