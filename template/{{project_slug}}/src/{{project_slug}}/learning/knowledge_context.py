"""
Shared institutional-knowledge context builder.

One place that renders what the platform has learned -- approved
claim corrections (learning/corrections.py, type="") and extracted
error schemas (learning/error_schemata.py) -- into a prompt-ready text
block. Governed procedures (type=procedure) are never included here;
an explicit opt-in render path is required. Used by all three
resolution tiers so learned knowledge grounds every path, not
just single-shot:

  - Tier 1 /resolve:            orchestration/single_shot.py
  - Tier 2 /chat:               api/routes/chat.py (synthesis context)
  - Tier 3 /round-table/tasks:  api/routes/round_table.py (task context)

Every lookup is best-effort: a failing store degrades the context to
whatever could be fetched, never the request.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_knowledge_context(
    corrections_manager: Any = None,
    learning_store: Any = None,
    tenant_id: str = "default",
    agent_id: str = "",
) -> str:
    """
    Render the tenant's learned knowledge as a prompt block.

    Combines approved corrections (four-eyes reviewed, budget-capped by
    the manager) and extracted error schemas. Returns "" when nothing is
    available or neither source is configured.
    """
    parts: list[str] = []

    if corrections_manager is not None:
        try:
            block = corrections_manager.get_approved_for_context(
                tenant_id=tenant_id, agent_id=agent_id
            )
            if block:
                parts.append(block)
        except Exception as exc:
            logger.warning("[KnowledgeContext] Corrections context failed: %s", exc)

    if learning_store is not None:
        try:
            from .error_schemata import get_schemas_for_context

            block = get_schemas_for_context(
                learning_store, tenant_id=tenant_id, agent_id=agent_id
            )
            if block:
                parts.append(block)
        except Exception as exc:
            logger.warning("[KnowledgeContext] Error-schema context failed: %s", exc)

    return "\n\n".join(parts)
