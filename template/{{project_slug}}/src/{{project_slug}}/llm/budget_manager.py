"""
BudgetManager -- per-tenant LLM cost governance.

Tracks spend per tenant and answers "may this tenant make another LLM
call?" with three statuses: ALLOWED, WARNED (past the warn threshold),
EXHAUSTED (at or past the hard cap).

Persistence is optional: pass a LearningStore-compatible object (see
learning/store.py) and spend is written to the budget_spend table --
and budget caps to the budget_configs table -- so both survive
restarts. Spend totals are always read fresh from the store; cap
configs are cached per process and re-read from the store at most
every BUDGET_CONFIG_TTL_SECONDS (default 60), so a cap changed in one
process converges in the others within the TTL rather than instantly.
Negative lookups (tenants with no configured cap) are cached under the
same TTL, so unconfigured tenants do not hit the store on every LLM
call. Without a store, everything is tracked in-memory for the
process lifetime.

Reset semantics: changing a tenant's cap does NOT reset accumulated
spend. Spend is a monotonically growing ledger (budget_spend rows plus
any in-memory fallback); there is no time window and no reset endpoint.
To grant more headroom, raise the cap; to "reset", delete the tenant's
budget_spend rows out of band.

Windowed reads: when the store exposes the optional sum_amount
aggregate (both reference backends do), spend totals are computed with
SQL SUM instead of fetching every ledger row; custom stores without it
fall back to fetch-and-sum.

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
import os
import time
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

DEFAULT_WARN_AT = 0.8
DEFAULT_CONFIG_TTL_SECONDS = 60.0
SPEND_TABLE = "budget_spend"
CONFIG_TABLE = "budget_configs"


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
            f"Raise the budget cap (changing the cap does not reset "
            f"recorded spend)."
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
        # Store-backed cap configs are re-read at most once per TTL;
        # tenants present here but absent from _budgets are cached
        # negative lookups (no cap configured).
        self._config_read_at: dict[str, float] = {}
        self._config_ttl = float(
            os.getenv("BUDGET_CONFIG_TTL_SECONDS", str(DEFAULT_CONFIG_TTL_SECONDS))
        )

    def set_budget(
        self, tenant_id: str, max_budget_usd: float, warn_at: float = DEFAULT_WARN_AT
    ) -> dict:
        """Set (or replace) a tenant's budget. Returns the stored config.

        Persists to the budget_configs table when a store is configured,
        so caps survive restarts alongside the spend ledger (best-effort:
        a failed write logs a warning and keeps the in-memory cap).
        Changing the cap does NOT reset accumulated spend.
        """
        max_budget_usd = float(max_budget_usd)
        if max_budget_usd < 0:
            raise ValueError("max_budget_usd must be >= 0 (0 = unlimited)")
        if not 0 < warn_at <= 1:
            raise ValueError("warn_at must be in (0, 1]")
        self._budgets[tenant_id] = {
            "max_budget_usd": max_budget_usd,
            "warn_at": float(warn_at),
        }
        self._config_read_at[tenant_id] = time.monotonic()
        self._persist_config(tenant_id)
        logger.info(
            f"[Budget] Set budget for '{tenant_id}': "
            f"${max_budget_usd:.2f} (warn at {warn_at:.0%})"
        )
        return self.get_budget(tenant_id)

    def _persist_config(self, tenant_id: str) -> None:
        """Best-effort write of a tenant's cap to budget_configs."""
        if self._store is None:
            return
        config = self._budgets[tenant_id]
        try:
            changes = {
                "max_budget_usd": config["max_budget_usd"],
                "warn_at": config["warn_at"],
                "updated_at": datetime.now().isoformat(),
            }
            if self._store.update(CONFIG_TABLE, tenant_id, changes):
                return
            try:
                self._store.insert(
                    CONFIG_TABLE,
                    {"id": tenant_id, "tenant_id": tenant_id, **changes},
                )
            except Exception:
                # Two processes can race the first insert for a tenant;
                # the loser hits the PK conflict. Converge by updating
                # the row the winner just created.
                if not self._store.update(CONFIG_TABLE, tenant_id, changes):
                    raise
        except Exception as e:
            logger.warning(
                f"[Budget] Config persist failed ({type(e).__name__}); "
                f"cap for '{tenant_id}' applies in this process but was "
                f"NOT persisted -- other processes and restarts will not "
                f"see it until a later set_budget succeeds"
            )

    def _load_config(self, tenant_id: str) -> dict | None:
        """Load a tenant's persisted cap (None when absent or store-less)."""
        if self._store is None:
            return None
        try:
            rows = self._store.query(CONFIG_TABLE, {"id": tenant_id}, limit=1)
        except Exception as e:
            logger.warning(
                f"[Budget] Config load failed ({type(e).__name__}); "
                f"using in-memory/default cap for '{tenant_id}'"
            )
            return None
        if not rows:
            return None
        return {
            "max_budget_usd": float(rows[0].get("max_budget_usd") or 0.0),
            "warn_at": float(rows[0].get("warn_at") or DEFAULT_WARN_AT),
        }

    def get_budget(self, tenant_id: str) -> dict:
        """Effective budget config for a tenant (falls back to the default).

        With a store, the persisted cap (budget_configs) is loaded and
        cached, then re-read at most once per BUDGET_CONFIG_TTL_SECONDS
        so cap changes made by other processes converge within the TTL.
        Negative lookups are cached under the same TTL. A re-read that
        finds no row (or fails) keeps the current cached value, so a cap
        whose persist failed still applies in this process.
        """
        if self._store is not None:
            now = time.monotonic()
            read_at = self._config_read_at.get(tenant_id)
            if read_at is None or now - read_at >= self._config_ttl:
                loaded = self._load_config(tenant_id)
                self._config_read_at[tenant_id] = now
                if loaded is not None:
                    self._budgets[tenant_id] = loaded
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

    def current_spend(self, tenant_id: str, since_iso: str = "") -> float:
        """Total recorded spend for a tenant (store rows + in-memory fallback).

        since_iso optionally windows the store-side total to rows with
        created_at >= since_iso (the default, "", keeps today's behavior:
        the full ledger). Uses the store's optional sum_amount aggregate
        (SQL SUM) when present; custom stores without it fall back to
        fetching the tenant's rows and summing in Python.
        """
        total = self._memory_spend.get(tenant_id, 0.0)
        if self._store is not None:
            try:
                sum_amount = getattr(self._store, "sum_amount", None)
                if callable(sum_amount):
                    total += float(
                        sum_amount(
                            SPEND_TABLE,
                            "amount_usd",
                            {"tenant_id": tenant_id},
                            since_iso=since_iso,
                        )
                        or 0.0
                    )
                else:
                    rows = self._store.query(SPEND_TABLE, {"tenant_id": tenant_id})
                    total += sum(
                        float(r.get("amount_usd") or 0.0)
                        for r in rows
                        if not since_iso or r.get("created_at", "") >= since_iso
                    )
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
