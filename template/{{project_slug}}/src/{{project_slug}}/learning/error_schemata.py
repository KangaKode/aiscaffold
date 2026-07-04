"""
Error schemata -- deterministic generalization of correction clusters.

When an agent keeps getting corrected on the same theme, the individual
corrections can be generalized into a reusable "error schema": a compact
description of the recurring mistake plus mitigation steps, distilled from
the corrections themselves. Schemas are injected into single-shot
resolution context alongside raw corrections, so one schema can replace
many near-duplicate correction entries.

Extraction is deterministic templating over structured fields -- no LLM,
no embeddings, $0 ongoing cost. Safeguards:
  - a cluster needs >= MIN_CLUSTER_SIZE approved corrections
  - corrections must come from >= MIN_DISTINCT_CREATORS different proposers
    (one user cannot single-handedly shape an agent's standing guidance)
  - re-running extraction updates the existing schema instead of duplicating

Rendered schema text passes through sanitize_for_prompt before it reaches
any prompt, same as corrections.

Keep this file under 250 lines.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..security.prompt_guard import sanitize_for_prompt
from .corrections import STATUS_APPROVED
from .store import LearningStore

logger = logging.getLogger(__name__)

MIN_CLUSTER_SIZE = 3
MIN_DISTINCT_CREATORS = 2
MAX_MITIGATION_STEPS = 5
OVERLAP_DEDUP_THRESHOLD = 0.80
DEFAULT_SCHEMA_BUDGET_CHARS = 2000
MAX_FIELD_RENDER_CHARS = 400

STATUS_ACTIVE = "active"
STATUS_RETIRED_SCHEMA = "retired"


@dataclass
class ErrorSchema:
    """A generalized recurring-error pattern -- mirrors the error_schemas table."""

    tenant_id: str = "default"
    agent_id: str = ""
    evidence_level: str = ""
    title: str = ""
    description: str = ""
    mitigation_steps: list[str] = field(default_factory=list)
    source_correction_ids: list[str] = field(default_factory=list)
    status: str = STATUS_ACTIVE
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


def _to_row(s: ErrorSchema) -> dict:
    return {
        "id": s.id,
        "tenant_id": s.tenant_id,
        "agent_id": s.agent_id,
        "evidence_level": s.evidence_level,
        "title": s.title,
        "description": s.description,
        "mitigation_steps_json": json.dumps(s.mitigation_steps, default=str),
        "source_correction_ids_json": json.dumps(s.source_correction_ids),
        "status": s.status,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


def _dedup_by_overlap(texts: list[str], threshold: float = OVERLAP_DEDUP_THRESHOLD) -> list[str]:
    """Drop texts whose word-set overlap with an earlier entry exceeds threshold."""
    result: list[str] = []
    for text in texts:
        words = set(text.lower().split())
        if not words:
            continue
        duplicate = False
        for existing in result:
            existing_words = set(existing.lower().split())
            if len(words & existing_words) / max(len(words), 1) > threshold:
                duplicate = True
                break
        if not duplicate:
            result.append(text)
    return result


def extract_error_schemas(store: LearningStore, tenant_id: str = "default") -> list[ErrorSchema]:
    """Generalize a tenant's approved corrections into error schemas.

    Groups approved corrections by (agent_id, evidence_level); every group
    passing the cluster-size and distinct-creator safeguards becomes one
    schema. Idempotent: an existing active schema for the same group is
    updated in place.
    """
    rows = store.query(
        "corrections",
        {"tenant_id": tenant_id, "status": STATUS_APPROVED},
        order_by="created_at DESC",
    )

    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (row.get("agent_id") or "", row.get("evidence_level") or "")
        groups.setdefault(key, []).append(row)

    schemas: list[ErrorSchema] = []
    for (agent_id, evidence_level), members in groups.items():
        if len(members) < MIN_CLUSTER_SIZE:
            continue
        creators = {m.get("created_by") or "" for m in members} - {""}
        if len(creators) < MIN_DISTINCT_CREATORS:
            logger.info(
                "[ErrorSchemata] Skipping cluster agent=%s: only %d distinct creator(s)",
                agent_id, len(creators),
            )
            continue

        reasons = _dedup_by_overlap([m.get("reason") or "" for m in members])
        steps = _dedup_by_overlap([m.get("corrected_claim") or "" for m in members])
        label = agent_id or "all agents"
        schema = ErrorSchema(
            tenant_id=tenant_id,
            agent_id=agent_id,
            evidence_level=evidence_level,
            title=f"Recurring corrections for {label} ({len(members)} approved)",
            description=(
                f"{len(members)} approved corrections share this pattern. "
                f"Common reasons: {'; '.join(r[:200] for r in reasons[:3])}"
            ),
            mitigation_steps=steps[:MAX_MITIGATION_STEPS],
            source_correction_ids=[m.get("id", "") for m in members],
        )

        existing = store.query(
            "error_schemas",
            {
                "tenant_id": tenant_id,
                "agent_id": agent_id,
                "evidence_level": evidence_level,
                "status": STATUS_ACTIVE,
            },
            limit=1,
        )
        if existing:
            schema.id = existing[0]["id"]
            schema.created_at = existing[0].get("created_at", schema.created_at)
            row = _to_row(schema)
            row.pop("id")
            store.update("error_schemas", schema.id, row)
        else:
            store.insert("error_schemas", _to_row(schema))
        schemas.append(schema)

    if schemas:
        logger.info(
            "[ErrorSchemata] Extracted %d schema(s) for tenant '%s'",
            len(schemas), tenant_id,
        )
    return schemas


def get_schemas_for_context(
    store: Any,
    tenant_id: str = "default",
    agent_id: str = "",
    budget_chars: int = DEFAULT_SCHEMA_BUDGET_CHARS,
) -> str:
    """Render active error schemas as a prompt block ("" when none).

    Stops adding entries BEFORE the block would exceed budget_chars. All
    stored text is sanitized before rendering, same as corrections.
    """
    if store is None:
        return ""
    filters: dict[str, Any] = {"tenant_id": tenant_id, "status": STATUS_ACTIVE}
    if agent_id:
        filters["agent_id"] = agent_id
    try:
        rows = store.query("error_schemas", filters, order_by="updated_at DESC")
    except Exception as exc:
        logger.warning("[ErrorSchemata] Context query failed: %s", exc)
        return ""
    if not rows:
        return ""

    header = "## Known error patterns (avoid repeating these mistakes)\n"
    block = header
    added = 0
    for row in rows:
        title = sanitize_for_prompt(
            row.get("title", ""), max_length=MAX_FIELD_RENDER_CHARS
        )
        description = sanitize_for_prompt(
            row.get("description", ""), max_length=MAX_FIELD_RENDER_CHARS
        )
        try:
            steps = json.loads(row.get("mitigation_steps_json") or "[]")
        except json.JSONDecodeError:
            steps = []
        entry = f"- {title}\n  {description}\n"
        for step in steps[:MAX_MITIGATION_STEPS]:
            entry += (
                f"  * {sanitize_for_prompt(str(step), max_length=MAX_FIELD_RENDER_CHARS)}\n"
            )
        if len(block) + len(entry) > budget_chars:
            break
        block += entry
        added += 1

    return block.rstrip() if added else ""
