"""
Reflections API -- read process-level lessons from past deliberations.

  GET /api/v1/reflections?reflection_type=&limit= -- List a tenant's
      reflections, newest first.

Reflections are produced automatically by the Reflector
(learning/reflector.py) after each round table run: deterministic,
structured observations about HOW the deliberation worked (agent
effectiveness, evidence patterns, challenge impact, dissent value).
This endpoint is read-only -- reflections are never written over HTTP.

Security:
  - Auth required (same bearer scheme as the other routes)
  - Results are tenant-scoped via the AuthContext
  - Requires app.state.learning_store (503 when not configured)
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ...learning.reflector import ReflectionType
from ...security import ValidationError, validate_in_choices
from ..middleware.auth import AuthContext, verify_api_key

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_LIST_LIMIT = 500


@router.get("/reflections")
async def list_reflections(
    request: Request,
    reflection_type: str = "",
    limit: int = 100,
    auth: AuthContext = Depends(verify_api_key),
) -> dict:
    """List the caller's tenant reflections, newest first."""
    store = getattr(request.app.state, "learning_store", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Reflections not available (learning store not configured)",
        )

    if reflection_type:
        try:
            validate_in_choices(
                reflection_type, list(ReflectionType.ALL), field_name="reflection_type"
            )
        except ValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

    filters = {"tenant_id": auth.tenant_id}
    if reflection_type:
        filters["reflection_type"] = reflection_type

    rows = store.query(
        "reflections",
        filters,
        order_by="created_at DESC",
        limit=max(1, min(limit, MAX_LIST_LIMIT)),
    )

    reflections = []
    for row in rows:
        try:
            metrics = json.loads(row.get("quality_metrics_json") or "{}")
        except json.JSONDecodeError:
            metrics = {}
        reflections.append(
            {
                "id": row.get("id", ""),
                "source_task_id": row.get("source_task_id", ""),
                "reflection_type": row.get("reflection_type", ""),
                "title": row.get("title", ""),
                "detail": row.get("detail", ""),
                "quality_metrics": metrics,
                "status": row.get("status", ""),
                "created_at": row.get("created_at", ""),
            }
        )
    return {"reflections": reflections, "total": len(reflections)}
