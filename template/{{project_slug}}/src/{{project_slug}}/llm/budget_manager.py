"""
BudgetManager -- per-tenant LLM cost governance.

Tracks spend per tenant and answers "may this tenant make another LLM
call?" with three statuses: ALLOWED, WARNED (past the warn threshold),
EXHAUSTED (at or past the hard cap).

Persistence is optional: pass a LearningStore-compatible object (see
learning/store.py) and spend is written to the budget_spend table so it
survives restarts and is shared across processes. Without a store,
spend is tracked in-memory for the process lifetime.

Tenant identity flows through a contextvar: orchestration entry points
call set_tenant_context(tenant_id), and the LLM client reads it back
with get_tenant_context() -- no need to thread tenant_id through every
call site. The default tenant is "default".

Zero-config behavior: if no BudgetManager is wired into the LLM client,
no checks happen and nothing changes.

Keep this file under 500 lines.
"""

import contextvars
import logging
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

DEFAULT_WARN_AT = 0.8
SPEND_TABLE = "budget_spend"


class BudgetStatus:
    """Budget check outcomes (plain strings for easy comparison/serialization)."""

    ALLOWED = "allowed"
    WARNED = "warned"
    EXHAUSTED = "exhausted"


class BudgetExceededError(Exception):
    """Raised when a call is blocked because the tenant's budget is exhausted."""

    def __init__(
        self,
        tenant_id: str,
        current_spend: float = 0.0,
        max_budget_usd: float = 0.0,
    ):
        self.tenant_id = tenant_id
        self.current_spend = current_spend
        self.max_budget_usd = max_budget_usd
        super().__init__(
            f"LLM budget exhausted for tenant '{tenant_id}': "
            f"${current_spend:.4f} spent of ${max_budget_usd:.4f} allowed. "
            f"Raise the budget or wait for it to be reset."
        )


# =============================================================================
# TENANT CONTEXT (contextvars -- async-safe propagation)
# =============================================================================

_tenant_context: contextvars.ContextVar[str] = contextvars.ContextVar(
    "llm_tenant_id", default="default"
)


def set_tenant_context(tenant_id: str) -> contextvars.Token:
    """Set the current tenant. Call at orchestration entry points.

    Returns the contextvars token so callers that need strict scoping can
    reset it; most callers can ignore the return value.
    """
    return _tenant_context.set(tenant_id or "default")


def get_tenant_context() -> str:
    """Current tenant id from async context ("default" when unset)."""
    return _tenant_context.get()


# =============================================================================
# BUDGET MANAGER
# =============================================================================


class BudgetManager:
    """
    Per-tenant spend tracking with warn/exhaust thresholds.

    Usage:
        manager = BudgetManager(store=get_learning_store())
        manager.set_budget("acme", max_budget_usd=10.0, warn_at=0.8)
        manager.record_spend("acme", 0.42, model="some-model")
        if manager.check("acme") == BudgetStatus.EXHAUSTED:
            ...block the call...

    A budget of 0 means unlimited (checks always return ALLOWED).
    """

    def __init__(self, store=None, default_budget_usd: float = 0.0):
        self._store = store
        self._default_budget_usd = float(default_budget_usd)
        self._budgets: dict[str, dict] = {}
        self._memory_spend: dict[str, float] = {}

    def set_budget(
        self, tenant_id: str, max_budget_usd: float, warn_at: float = DEFAULT_WARN_AT
    ) -> dict:
        """Set (or replace) a tenant's budget. Returns the stored config."""
        max_budget_usd = float(max_budget_usd)
        if max_budget_usd < 0:
            raise ValueError("max_budget_usd must be >= 0 (0 = unlimited)")
        if not 0 < warn_at <= 1:
            raise ValueError("warn_at must be in (0, 1]")
        self._budgets[tenant_id] = {
            "max_budget_usd": max_budget_usd,
            "warn_at": float(warn_at),
        }
        logger.info(
            f"[Budget] Set budget for '{tenant_id}': "
            f"${max_budget_usd:.2f} (warn at {warn_at:.0%})"
        )
        return self.get_budget(tenant_id)

    def get_budget(self, tenant_id: str) -> dict:
        """Effective budget config for a tenant (falls back to the default)."""
        configured = self._budgets.get(tenant_id)
        if configured is not None:
            max_budget = configured["max_budget_usd"]
            warn_at = configured["warn_at"]
        else:
            max_budget = self._default_budget_usd
            warn_at = DEFAULT_WARN_AT
        return {
            "tenant_id": tenant_id,
            "max_budget_usd": max_budget,
            "warn_at": warn_at,
        }

    def record_spend(
        self, tenant_id: str, amount_usd: float, model: str = ""
    ) -> None:
        """Record spend for a tenant. Best-effort: never raises.

        Persists to the budget_spend table when a store is configured;
        otherwise accumulates in-memory. Negative amounts are ignored.
        """
        amount_usd = float(amount_usd)
        if amount_usd < 0:
            logger.warning(
                f"[Budget] Ignoring negative spend ${amount_usd:.4f} "
                f"for tenant '{tenant_id}'"
            )
            return

        if self._store is not None:
            try:
                self._store.insert(SPEND_TABLE, {
                    "id": uuid.uuid4().hex,
                    "tenant_id": tenant_id,
                    "amount_usd": amount_usd,
                    "model": model,
                    "created_at": datetime.now().isoformat(),
                })
                return
            except Exception as e:
                logger.warning(
                    f"[Budget] Store write failed ({type(e).__name__}); "
                    f"falling back to in-memory spend for '{tenant_id}'"
                )

        self._memory_spend[tenant_id] = (
            self._memory_spend.get(tenant_id, 0.0) + amount_usd
        )

    def current_spend(self, tenant_id: str) -> float:
        """Total recorded spend for a tenant (store rows + in-memory fallback)."""
        total = self._memory_spend.get(tenant_id, 0.0)
        if self._store is not None:
            try:
                rows = self._store.query(SPEND_TABLE, {"tenant_id": tenant_id})
                total += sum(float(r.get("amount_usd") or 0.0) for r in rows)
            except Exception as e:
                logger.warning(
                    f"[Budget] Store read failed ({type(e).__name__}); "
                    f"reporting in-memory spend only for '{tenant_id}'"
                )
        return total

    def remaining_pct(self, tenant_id: str) -> float | None:
        """Fraction of budget remaining in [0, 1], or None when unlimited.

        Used by the model router for budget-aware tier downgrades. Unlimited
        budgets (max 0) return None so the router leaves routing untouched.
        Any error returns None (fail-open: do not perturb routing).
        """
        try:
            max_budget = self.get_budget(tenant_id)["max_budget_usd"]
            if max_budget <= 0:
                return None
            remaining = max_budget - self.current_spend(tenant_id)
            return max(0.0, min(1.0, remaining / max_budget))
        except Exception:
            return None

    def check(self, tenant_id: str) -> str:
        """Budget status for a tenant: ALLOWED, WARNED, or EXHAUSTED.

        Unlimited budgets (max 0) are always ALLOWED. Any error while
        computing status fails open (governance guardrail, not a
        security gate).
        """
        try:
            budget = self.get_budget(tenant_id)
            max_budget = budget["max_budget_usd"]
            if max_budget <= 0:
                return BudgetStatus.ALLOWED
            spend = self.current_spend(tenant_id)
            if spend >= max_budget:
                return BudgetStatus.EXHAUSTED
            if spend >= max_budget * budget["warn_at"]:
                return BudgetStatus.WARNED
            return BudgetStatus.ALLOWED
        except Exception as e:
            logger.warning(
                f"[Budget] Check failed for '{tenant_id}' "
                f"({type(e).__name__}); failing open"
            )
            return BudgetStatus.ALLOWED


# =============================================================================
# LLM CLIENT GLUE (kept here so client.py stays small)
# =============================================================================


def enforce_budget(manager: "BudgetManager | None") -> None:
    """Raise BudgetExceededError when the current tenant's budget is exhausted.

    No-op when manager is None, so unconfigured deployments never check.
    Call before making an LLM call.
    """
    if manager is None:
        return
    tenant_id = get_tenant_context()
    if manager.check(tenant_id) == BudgetStatus.EXHAUSTED:
        raise BudgetExceededError(
            tenant_id,
            current_spend=manager.current_spend(tenant_id),
            max_budget_usd=manager.get_budget(tenant_id)["max_budget_usd"],
        )


def record_response_spend(
    manager: "BudgetManager | None", amount_usd: float, model: str = ""
) -> None:
    """Best-effort spend recording for the current tenant after an LLM call.

    No-op when manager is None. Never raises.
    """
    if manager is None:
        return
    try:
        manager.record_spend(get_tenant_context(), amount_usd, model=model)
    except Exception as e:
        logger.warning(f"[Budget] Spend recording failed: {type(e).__name__}")
