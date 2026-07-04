"""
Learning table definitions -- single source of truth for the LearningStore.

Every table and column the extended learning system persists is declared
ONCE here. store.py validates all SQL identifiers against TABLE_COLUMNS
before interpolation; values only ever travel through parameterized
placeholders.

Schema changes go through MIGRATIONS (forward-only, tracked in the
schema_version table). Every statement must be idempotent
(IF NOT EXISTS) and valid on both SQLite and Postgres.

Keep this file under 250 lines.
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
    },
    "budget_spend": {
        "id": "TEXT PRIMARY KEY",
        "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        "amount_usd": "REAL DEFAULT 0",
        "model": "TEXT DEFAULT ''",
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
]

SCHEMA_VERSION_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_version"
    " (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
)


def _baseline_statements() -> list[str]:
    """DDL for migration version 1: create all tables and indexes."""
    statements = []
    for table, columns in TABLE_COLUMNS.items():
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
]
