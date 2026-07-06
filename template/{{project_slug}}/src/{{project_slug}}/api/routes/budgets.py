"""
Budgets API -- per-tenant LLM cost governance.

  GET /api/v1/budgets/{tenant_id} -- Budget config, current spend, and status
  PUT /api/v1/budgets/{tenant_id} -- Set or replace a tenant's budget

The budget manager is expected at app.state.budget_manager (wired at
startup). When absent, endpoints return 503 so callers can tell
"governance not enabled" apart from "tenant has no budget".

Security:
  - All endpoints require API key
  - tenant_id validated as a safe identifier
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...llm.budget_manager import BudgetManager
from ...security import ValidationError, validate_identifier
from ..middleware.auth import AuthContext, verify_api_key

logger = logging.getLogger(__name__)
router = APIRouter()


class BudgetUpdateRequest(BaseModel):
    """Request to set a tenant's budget."""

    max_budget_usd: float = Field(
        ..., ge=0, description="Hard spend cap in USD (0 = unlimited)"
    )
    warn_at: float = Field(
        0.8, gt=0, le=1, description="Warn threshold as a fraction of the cap"
    )


class BudgetResponse(BaseModel):
    """Budget config plus live spend for a tenant."""

    tenant_id: str
    max_budget_usd: float
    warn_at: float
    current_spend_usd: float
    status: str


def _get_budget_manager(request: Request) -> BudgetManager:
    manager = getattr(request.app.state, "budget_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Budget governance is not enabled: no budget manager is "
                "configured on this deployment (app.state.budget_manager)"
            ),
        )
    return manager


def _validated_tenant(tenant_id: str) -> str:
    try:
        validate_identifier(tenant_id, "tenant_id")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return tenant_id


def _budget_response(manager: BudgetManager, tenant_id: str) -> BudgetResponse:
    """Build the response (sync store reads -- call via asyncio.to_thread)."""
    budget = manager.get_budget(tenant_id)
    return BudgetResponse(
        tenant_id=tenant_id,
        max_budget_usd=budget["max_budget_usd"],
        warn_at=budget["warn_at"],
        current_spend_usd=manager.current_spend(tenant_id),
        status=manager.check(tenant_id),
    )


@router.get("/budgets/{tenant_id}", response_model=BudgetResponse)
async def get_budget(
    tenant_id: str,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> BudgetResponse:
    """Get a tenant's budget config, current spend, and status."""
    tenant_id = _validated_tenant(tenant_id)
    manager = _get_budget_manager(request)
    return await asyncio.to_thread(_budget_response, manager, tenant_id)


@router.put("/budgets/{tenant_id}", response_model=BudgetResponse)
async def set_budget(
    tenant_id: str,
    update: BudgetUpdateRequest,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> BudgetResponse:
    """Set (or replace) a tenant's budget cap and warn threshold.

    The cap persists to the budget_configs table when a store is
    configured, so it survives restarts alongside the spend ledger.
    Changing the cap does NOT reset accumulated spend.
    """
    tenant_id = _validated_tenant(tenant_id)
    manager = _get_budget_manager(request)
    await asyncio.to_thread(
        manager.set_budget,
        tenant_id,
        max_budget_usd=update.max_budget_usd,
        warn_at=update.warn_at,
    )
    logger.info(f"[BudgetsAPI] Budget updated for '{tenant_id}'")
    return await asyncio.to_thread(_budget_response, manager, tenant_id)
