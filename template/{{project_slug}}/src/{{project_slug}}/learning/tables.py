"""
Learning table definitions -- single source of truth for the LearningStore.

Every table and column the extended learning system persists is declared
ONCE here. store.py validates all SQL identifiers against TABLE_COLUMNS
before interpolation; values only ever travel through parameterized
placeholders.

Schema changes go through MIGRATIONS (forward-only, tracked in the
schema_version table). Every statement must be idempotent
(IF NOT EXISTS) or guarded by a new schema version (ALTER TABLE ADD
COLUMN), and valid on both SQLite and Postgres.

BASELINE FREEZE RULE: the v1 baseline DDL must keep creating the
ORIGINAL column set forever. When you add a column to an existing
table, (1) add it to TABLE_COLUMNS (insert/query validation), (2) add
a frozen pre-change snapshot of that table to _BASELINE_COLUMN_FREEZE
so the baseline keeps creating the old shape, and (3) append an ALTER
TABLE ADD COLUMN migration. If the baseline created the new column on
fresh installs, the version-guarded ALTER would then fail with a
duplicate-column error (SQLite has no ADD COLUMN IF NOT EXISTS).

Keep this file under 380 lines. (Raised from 300, then 350: migration v8
froze two more pre-change table snapshots; v9 adds corrections validity
columns + a filter/sort index. Freeze snapshots must live here by design,
the module is never split. Headroom reserved for B1's later typed column.)
"""

TABLE_COLUMNS: dict[str, dict[str, str]] = {
    "corrections": {
        "id": "TEXT PRIMARY KEY",
        "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        "agent_id": "TEXT DEFAULT ''",
        "session_id": "TEXT DEFAULT ''",
        "original_claim": "TEXT DEFAULT ''",
        "corrected_claim": "TEXT DEFAULT ''",
        "reason": "TEXT DEFAULT ''",
        "evidence_level": "TEXT DEFAULT ''",
        "status": "TEXT DEFAULT 'proposed'",
        "created_by": "TEXT DEFAULT ''",
        "approved_by": "TEXT DEFAULT ''",
        "created_at": "TEXT NOT NULL",
        "updated_at": "TEXT NOT NULL",
        "metadata_json": "TEXT DEFAULT '{}'",
        # Knowledge aging (added in migration v5 -- NOT in the v1 baseline)
        "last_validated_at": "TEXT DEFAULT ''",
        "last_validated_by": "TEXT DEFAULT ''",
        # Write provenance (added in migration v8): "api" for the
        # corrections API route, "library" for direct manager calls.
        "source_surface": "TEXT DEFAULT ''",
        # Validity + supersession (added in migration v9). Empty invalid_at
        # means currently valid (canonical sentinel -- store equality
        # filters reject None).
        "valid_at": "TEXT DEFAULT ''",
        "invalid_at": "TEXT DEFAULT ''",
        "supersedes_id": "TEXT DEFAULT ''",
    },
    "activity_events": {
        "id": "TEXT PRIMARY KEY",
        "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        "user_id": "TEXT DEFAULT ''",
        "route": "TEXT DEFAULT ''",
        "method": "TEXT DEFAULT ''",
        "status_code": "INTEGER DEFAULT 0",
        "created_at": "TEXT NOT NULL",
    },
    "agent_dispatch_stats": {
        "id": "TEXT PRIMARY KEY",
        "agent_id": "TEXT NOT NULL",
        "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        "dispatched_at": "TEXT NOT NULL",
        "duration_seconds": "REAL DEFAULT 0",
        "refused": "INTEGER DEFAULT 0",
        "confidence": "REAL DEFAULT 0",
        "scope_violations": "INTEGER DEFAULT 0",
    },
    "integrity_flags": {
        "id": "TEXT PRIMARY KEY",
        "flag_type": "TEXT NOT NULL",
        "subject_id": "TEXT DEFAULT ''",
        "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        "severity": "TEXT DEFAULT 'info'",
        "detail_json": "TEXT DEFAULT '{}'",
        "created_at": "TEXT NOT NULL",
        "resolved": "INTEGER DEFAULT 0",
    },
    "audit_events": {
        "id": "TEXT PRIMARY KEY",
        "correlation_id": "TEXT NOT NULL",
        "event_type": "TEXT NOT NULL",
        "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        "phase": "TEXT DEFAULT ''",
        "agent_count": "INTEGER DEFAULT 0",
        "duration_seconds": "REAL DEFAULT 0",
        "outcome": "TEXT DEFAULT ''",
        "detail_json": "TEXT DEFAULT '{}'",
        "created_at": "TEXT NOT NULL",
        # Acting user (added in migration v8 -- NOT in the v1 baseline).
        # Populated on API-driven writes going forward; historical rows
        # and library callers without a user identity leave it ''.
        "user_id": "TEXT DEFAULT ''",
    },
    "budget_spend": {
        "id": "TEXT PRIMARY KEY",
        "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        "amount_usd": "REAL DEFAULT 0",
        "model": "TEXT DEFAULT ''",
        "created_at": "TEXT NOT NULL",
    },
    # Per-tenant budget caps (added in migration v6 -- one row per tenant,
    # id == tenant_id). Caps survive restarts alongside the spend ledger.
    "budget_configs": {
        "id": "TEXT PRIMARY KEY",
        "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        "max_budget_usd": "REAL DEFAULT 0",
        "warn_at": "REAL DEFAULT 0.8",
        "updated_at": "TEXT NOT NULL",
    },
    "reflections": {
        "id": "TEXT PRIMARY KEY",
        "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        "source_task_id": "TEXT DEFAULT ''",
        "reflection_type": "TEXT NOT NULL",
        "title": "TEXT DEFAULT ''",
        "detail": "TEXT DEFAULT ''",
        "quality_metrics_json": "TEXT DEFAULT '{}'",
        "status": "TEXT DEFAULT 'recorded'",
        "created_at": "TEXT NOT NULL",
        # Acting user whose deliberation produced the reflection (added
        # in migration v8 -- NOT in the pre-v8 shape the v4 migration
        # creates). '' for library callers without a user identity.
        "created_by": "TEXT DEFAULT ''",
    },
    # Phase-derivation records (added in migration v8, opt-in via
    # DELEGATION_RECORDS_ENABLED). One row per gated dispatch:
    # derived_from_json names the upstream artifacts the dispatch
    # consumed (analyze: []; challenge: contributing analysts' names;
    # vote: ["synthesis"]). NOT agent-to-agent calls -- those do not
    # exist in this hub-and-spoke architecture.
    "delegation_records": {
        "id": "TEXT PRIMARY KEY",
        "correlation_id": "TEXT NOT NULL",
        "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        "phase": "TEXT NOT NULL",
        "agent_id": "TEXT NOT NULL",
        "derived_from_json": "TEXT DEFAULT '[]'",
        "created_at": "TEXT NOT NULL",
    },
    "error_schemas": {
        "id": "TEXT PRIMARY KEY",
        "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        "agent_id": "TEXT DEFAULT ''",
        "evidence_level": "TEXT DEFAULT ''",
        "title": "TEXT DEFAULT ''",
        "description": "TEXT DEFAULT ''",
        "mitigation_steps_json": "TEXT DEFAULT '[]'",
        "source_correction_ids_json": "TEXT DEFAULT '[]'",
        "status": "TEXT DEFAULT 'active'",
        "created_at": "TEXT NOT NULL",
        "updated_at": "TEXT NOT NULL",
    },
}

INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_corrections_tenant_status"
    " ON corrections(tenant_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_activity_tenant"
    " ON activity_events(tenant_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_dispatch_agent"
    " ON agent_dispatch_stats(agent_id, dispatched_at)",
    "CREATE INDEX IF NOT EXISTS idx_integrity_tenant"
    " ON integrity_flags(tenant_id, resolved, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_correlation"
    " ON audit_events(correlation_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_budget_spend_tenant"
    " ON budget_spend(tenant_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_reflections_tenant"
    " ON reflections(tenant_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_error_schemas_tenant"
    " ON error_schemas(tenant_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_delegation_tenant"
    " ON delegation_records(tenant_id, created_at)",
]

SCHEMA_VERSION_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_version"
    " (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
)

# Frozen v1 snapshots for tables whose live TABLE_COLUMNS entry has grown
# since the baseline shipped (see BASELINE FREEZE RULE in the module
# docstring). These are literal copies, NOT references into TABLE_COLUMNS:
# the baseline must keep creating the original column set so that the
# version-guarded ALTER migrations apply cleanly on fresh installs too.
_BASELINE_COLUMN_FREEZE: dict[str, dict[str, str]] = {
    "corrections": {
        "id": "TEXT PRIMARY KEY",
        "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        "agent_id": "TEXT DEFAULT ''",
        "session_id": "TEXT DEFAULT ''",
        "original_claim": "TEXT DEFAULT ''",
        "corrected_claim": "TEXT DEFAULT ''",
        "reason": "TEXT DEFAULT ''",
        "evidence_level": "TEXT DEFAULT ''",
        "status": "TEXT DEFAULT 'proposed'",
        "created_by": "TEXT DEFAULT ''",
        "approved_by": "TEXT DEFAULT ''",
        "created_at": "TEXT NOT NULL",
        "updated_at": "TEXT NOT NULL",
        "metadata_json": "TEXT DEFAULT '{}'",
    },
    # Pre-v8 shapes: the baseline must keep creating audit_events and
    # reflections WITHOUT the v8 identity columns so the v8 ALTERs
    # apply cleanly on fresh installs too.
    "audit_events": {
        "id": "TEXT PRIMARY KEY",
        "correlation_id": "TEXT NOT NULL",
        "event_type": "TEXT NOT NULL",
        "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        "phase": "TEXT DEFAULT ''",
        "agent_count": "INTEGER DEFAULT 0",
        "duration_seconds": "REAL DEFAULT 0",
        "outcome": "TEXT DEFAULT ''",
        "detail_json": "TEXT DEFAULT '{}'",
        "created_at": "TEXT NOT NULL",
    },
    "reflections": {
        "id": "TEXT PRIMARY KEY",
        "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        "source_task_id": "TEXT DEFAULT ''",
        "reflection_type": "TEXT NOT NULL",
        "title": "TEXT DEFAULT ''",
        "detail": "TEXT DEFAULT ''",
        "quality_metrics_json": "TEXT DEFAULT '{}'",
        "status": "TEXT DEFAULT 'recorded'",
        "created_at": "TEXT NOT NULL",
    },
}


def _baseline_statements() -> list[str]:
    """DDL for migration version 1: create all tables and indexes.

    Frozen tables use their _BASELINE_COLUMN_FREEZE snapshot; columns
    added after v1 arrive exclusively via later ALTER migrations.
    """
    statements = []
    for table, columns in TABLE_COLUMNS.items():
        columns = _BASELINE_COLUMN_FREEZE.get(table, columns)
        cols = ", ".join(f"{name} {ddl}" for name, ddl in columns.items())
        statements.append(f"CREATE TABLE IF NOT EXISTS {table} ({cols})")
    statements.extend(INDEX_STATEMENTS)
    return statements


def _table_statements(table: str) -> list[str]:
    """Idempotent CREATE TABLE for one table (for post-baseline migrations)."""
    cols = ", ".join(f"{name} {ddl}" for name, ddl in TABLE_COLUMNS[table].items())
    return [f"CREATE TABLE IF NOT EXISTS {table} ({cols})"]


# Forward-only migrations. MIGRATIONS[i] is version i+1. Every statement
# must be idempotent (IF NOT EXISTS / ADD COLUMN guarded by a new version)
# and valid on both SQLite and Postgres.
MIGRATIONS: list[list[str]] = [
    _baseline_statements(),
    # audit_events: deliberation audit trail (metadata-only). Baseline
    # already creates it on fresh installs; this upgrades existing DBs.
    _table_statements("audit_events")
    + [
        "CREATE INDEX IF NOT EXISTS idx_audit_correlation"
        " ON audit_events(correlation_id, created_at)",
    ],
    # budget_spend: per-tenant LLM cost ledger. Baseline already creates
    # it on fresh installs; this upgrades existing DBs.
    _table_statements("budget_spend")
    + [
        "CREATE INDEX IF NOT EXISTS idx_budget_spend_tenant"
        " ON budget_spend(tenant_id, created_at)",
    ],
    # reflections + error_schemas: learning maturity pack. Baseline
    # already creates them on fresh installs; this upgrades existing DBs.
    _table_statements("reflections")
    + _table_statements("error_schemas")
    + [
        "CREATE INDEX IF NOT EXISTS idx_reflections_tenant"
        " ON reflections(tenant_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_error_schemas_tenant"
        " ON error_schemas(tenant_id, status, created_at)",
    ],
    # v5 -- corrections knowledge aging (last_validated_at/by). The v1
    # baseline is frozen at the pre-aging column set (see
    # _BASELINE_COLUMN_FREEZE), so this ALTER runs exactly once on both
    # fresh installs and upgraded databases.
    [
        "ALTER TABLE corrections ADD COLUMN last_validated_at TEXT DEFAULT ''",
        "ALTER TABLE corrections ADD COLUMN last_validated_by TEXT DEFAULT ''",
    ],
    # v6 -- budget_configs: per-tenant budget caps persist alongside the
    # spend ledger. New table (no frozen-table columns touched): baseline
    # already creates it on fresh installs; this upgrades existing DBs.
    _table_statements("budget_configs"),
    # v7 -- plain created_at index on activity_events: retention pruning
    # (learning/retention.py) scans oldest-first with no tenant filter,
    # which the composite idx_activity_tenant cannot serve. Index-only
    # (no frozen-table columns touched); IF NOT EXISTS keeps it
    # idempotent on both backends. Deliberately NOT in INDEX_STATEMENTS:
    # fresh installs replay every migration, so they get it here too,
    # and a DB stamped at v6 genuinely lacks it (which is what the
    # upgrade test asserts).
    [
        "CREATE INDEX IF NOT EXISTS idx_activity_created"
        " ON activity_events(created_at)",
    ],
    # v8 -- identity/provenance columns + delegation records.
    # ALTERs: the pre-v8 shapes of audit_events and reflections are
    # frozen in _BASELINE_COLUMN_FREEZE (and corrections was frozen at
    # v1), so these ALTERs are convergent rather than exactly-once: on
    # fresh installs and freeze-respecting upgrades they add the column;
    # on old v1-stamped DBs whose v2/v4 replays created the tables at
    # live shape, they land on the duplicate-column tolerance path and
    # are skipped. delegation_records is a new table (budget_configs/v6
    # precedent): the baseline already creates it on fresh installs;
    # this creates it on upgraded DBs.
    [
        "ALTER TABLE audit_events ADD COLUMN user_id TEXT DEFAULT ''",
        "ALTER TABLE corrections ADD COLUMN source_surface TEXT DEFAULT ''",
        "ALTER TABLE reflections ADD COLUMN created_by TEXT DEFAULT ''",
    ]
    + _table_statements("delegation_records")
    + [
        "CREATE INDEX IF NOT EXISTS idx_delegation_tenant"
        " ON delegation_records(tenant_id, created_at)",
    ],
    # v9 -- corrections validity + supersession. The v1 baseline is frozen
    # at the pre-B7 column set (see _BASELINE_COLUMN_FREEZE), so these
    # ALTERs run exactly once on both fresh installs and upgraded
    # databases (same pattern as v5 aging columns). Index deliberately
    # NOT in INDEX_STATEMENTS (v7 precedent) so a DB stamped at v8 gains
    # it via ensure_schema and the upgrade test can assert that.
    [
        "ALTER TABLE corrections ADD COLUMN valid_at TEXT DEFAULT ''",
        "ALTER TABLE corrections ADD COLUMN invalid_at TEXT DEFAULT ''",
        "ALTER TABLE corrections ADD COLUMN supersedes_id TEXT DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_corrections_tenant_status_valid"
        " ON corrections(tenant_id, status, invalid_at, created_at)",
    ],
]
