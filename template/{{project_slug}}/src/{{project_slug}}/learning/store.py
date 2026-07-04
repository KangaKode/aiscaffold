"""
LearningStore -- pluggable persistence for extended learning tables.

Defines a minimal storage protocol plus two reference backends:
  - SqliteLearningStore: stdlib sqlite3, zero extra dependencies (default)
  - PostgresLearningStore: psycopg 3, for shared/enterprise deployments
    (install with the 'postgres' extra: pip install "<project>[postgres]")

The protocol is intentionally tiny (ensure_schema / insert / query /
update / count / delete) so enterprise users can implement it against
anything (MySQL, DynamoDB, an internal storage service) without touching
callers.

Table names and columns are defined ONCE in tables.py (TABLE_COLUMNS).
Every SQL identifier is validated against that allowlist before
interpolation; values only ever travel through parameterized placeholders.
Schema changes go through MIGRATIONS in tables.py (forward-only, tracked
in the schema_version table).

Backend selection: get_learning_store() reads LEARNING_BACKEND
("sqlite" default | "postgres") and LEARNING_POSTGRES_DSN.

Keep this file under 400 lines.
"""

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .schema import DEFAULT_DB_PATH
from .tables import (  # noqa: F401 (re-exported for callers/tests)
    INDEX_STATEMENTS,
    MIGRATIONS,
    SCHEMA_VERSION_DDL,
    TABLE_COLUMNS,
)

logger = logging.getLogger(__name__)


# =============================================================================
# PROTOCOL
# =============================================================================


class LearningStore(Protocol):
    """
    Minimal storage interface for the extended learning tables.

    Implement these six methods against any datastore to bring your own
    backend (the scaffold ships SQLite and Postgres reference
    implementations). Rows are plain dicts keyed by column name; JSON
    columns (*_json) are stored as serialized strings by callers.
    """

    def ensure_schema(self) -> None:
        """Create tables / apply pending migrations. Must be idempotent."""
        ...

    def insert(self, table: str, row: dict) -> None:
        """Insert one row. Keys must match the table's columns."""
        ...

    def query(
        self, table: str, filters: dict, order_by: str = "", limit: int = 0
    ) -> list[dict]:
        """Fetch rows matching all equality filters. order_by is
        "column" or "column DESC"; limit 0 means no limit."""
        ...

    def update(self, table: str, row_id: str, changes: dict) -> bool:
        """Update the row with the given id. Returns True if a row changed."""
        ...

    def count(self, table: str, filters: dict) -> int:
        """Count rows matching all equality filters."""
        ...

    def delete(self, table: str, row_id: str) -> bool:
        """Hard-delete the row with the given id. Returns True if a row
        was removed. Used by GDPR erasure -- must actually remove data,
        not soft-delete."""
        ...


# =============================================================================
# SQL BUILDING (shared, allowlist-validated identifiers)
# =============================================================================


def _validate_table(table: str) -> None:
    if table not in TABLE_COLUMNS:
        raise ValueError(
            f"Unknown table {table!r}; allowed: {sorted(TABLE_COLUMNS)}"
        )


def _validate_columns(table: str, names) -> None:
    unknown = set(names) - set(TABLE_COLUMNS[table])
    if unknown:
        raise ValueError(f"Unknown columns for {table!r}: {sorted(unknown)}")


def _order_clause(table: str, order_by: str) -> str:
    """Validate 'column' or 'column ASC|DESC' against the allowlist."""
    if not order_by:
        return ""
    parts = order_by.split()
    if len(parts) > 2 or (len(parts) == 2 and parts[1].upper() not in ("ASC", "DESC")):
        raise ValueError(f"Invalid order_by: {order_by!r}")
    _validate_columns(table, [parts[0]])
    direction = f" {parts[1].upper()}" if len(parts) == 2 else ""
    return f" ORDER BY {parts[0]}{direction}"


def _insert_sql(table: str, row: dict, ph: str) -> tuple[str, list]:
    _validate_table(table)
    _validate_columns(table, row.keys())
    cols = list(row.keys())
    # Identifiers are allowlist-validated above; values go through
    # placeholders only, so string-building here is injection-safe.
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)})"  # nosec B608
        f" VALUES ({', '.join([ph] * len(cols))})"
    )
    return sql, [row[c] for c in cols]


def _where_sql(table: str, filters: dict, ph: str) -> tuple[str, list]:
    _validate_columns(table, filters.keys())
    if not filters:
        return "", []
    clause = " WHERE " + " AND ".join(f"{c} = {ph}" for c in filters)
    return clause, list(filters.values())


def _select_sql(
    table: str, filters: dict, order_by: str, limit: int, ph: str
) -> tuple[str, list]:
    _validate_table(table)
    where, values = _where_sql(table, filters, ph)
    # Identifiers allowlist-validated; values parameterized.
    sql = f"SELECT * FROM {table}{where}{_order_clause(table, order_by)}"  # nosec B608
    if limit:
        sql += f" LIMIT {int(limit)}"
    return sql, values


def _update_sql(table: str, row_id: str, changes: dict, ph: str) -> tuple[str, list]:
    _validate_table(table)
    _validate_columns(table, changes.keys())
    if not changes:
        raise ValueError("update() requires at least one change")
    sets = ", ".join(f"{c} = {ph}" for c in changes)
    # Identifiers allowlist-validated; values parameterized.
    sql = f"UPDATE {table} SET {sets} WHERE id = {ph}"  # nosec B608
    return sql, [*changes.values(), row_id]


def _count_sql(table: str, filters: dict, ph: str) -> tuple[str, list]:
    _validate_table(table)
    where, values = _where_sql(table, filters, ph)
    # Identifiers allowlist-validated; values parameterized.
    return f"SELECT COUNT(*) AS n FROM {table}{where}", values  # nosec B608


def _delete_sql(table: str, row_id: str, ph: str) -> tuple[str, list]:
    _validate_table(table)
    # Identifier allowlist-validated; value parameterized.
    return f"DELETE FROM {table} WHERE id = {ph}", [row_id]  # nosec B608


# =============================================================================
# SQLITE BACKEND (default)
# =============================================================================


class SqliteLearningStore:
    """Stdlib sqlite3 backend with WAL mode. Zero extra dependencies."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self._db_path = Path(db_path)
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute(SCHEMA_VERSION_DDL)
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current = row[0] or 0
            for version, statements in enumerate(MIGRATIONS, start=1):
                if version <= current:
                    continue
                for stmt in statements:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, datetime.now().isoformat()),
                )
                logger.info(f"[LearningStore] Applied migration v{version} (sqlite)")
            conn.commit()
        finally:
            conn.close()

    def insert(self, table: str, row: dict) -> None:
        sql, values = _insert_sql(table, row, "?")
        conn = self._connect()
        try:
            conn.execute(sql, values)
            conn.commit()
        finally:
            conn.close()

    def query(
        self, table: str, filters: dict, order_by: str = "", limit: int = 0
    ) -> list[dict]:
        sql, values = _select_sql(table, filters, order_by, limit, "?")
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(sql, values).fetchall()]
        finally:
            conn.close()

    def update(self, table: str, row_id: str, changes: dict) -> bool:
        sql, values = _update_sql(table, row_id, changes, "?")
        conn = self._connect()
        try:
            conn.execute(sql, values)
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def count(self, table: str, filters: dict) -> int:
        sql, values = _count_sql(table, filters, "?")
        conn = self._connect()
        try:
            row = conn.execute(sql, values).fetchone()
            return row["n"] if row else 0
        finally:
            conn.close()

    def delete(self, table: str, row_id: str) -> bool:
        sql, values = _delete_sql(table, row_id, "?")
        conn = self._connect()
        try:
            cursor = conn.execute(sql, values)
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


# =============================================================================
# POSTGRES BACKEND (optional -- 'postgres' extra)
# =============================================================================


class PostgresLearningStore:
    """
    psycopg 3 backend. Behavior parity with SqliteLearningStore: same
    allowlist validation, same migration list, %s placeholders.
    """

    def __init__(self, dsn: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "PostgresLearningStore requires psycopg 3; install with the"
                " postgres extra: pip install '<project>[postgres]'"
            ) from exc
        self._conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(SCHEMA_VERSION_DDL)
            cur.execute("SELECT MAX(version) AS v FROM schema_version")
            current = cur.fetchone()["v"] or 0
            for version, statements in enumerate(MIGRATIONS, start=1):
                if version <= current:
                    continue
                for stmt in statements:
                    cur.execute(stmt)
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (%s, %s)",
                    (version, datetime.now().isoformat()),
                )
                logger.info(f"[LearningStore] Applied migration v{version} (postgres)")

    def insert(self, table: str, row: dict) -> None:
        sql, values = _insert_sql(table, row, "%s")
        with self._conn.cursor() as cur:
            cur.execute(sql, values)

    def query(
        self, table: str, filters: dict, order_by: str = "", limit: int = 0
    ) -> list[dict]:
        sql, values = _select_sql(table, filters, order_by, limit, "%s")
        with self._conn.cursor() as cur:
            cur.execute(sql, values)
            return [dict(r) for r in cur.fetchall()]

    def update(self, table: str, row_id: str, changes: dict) -> bool:
        sql, values = _update_sql(table, row_id, changes, "%s")
        with self._conn.cursor() as cur:
            cur.execute(sql, values)
            return cur.rowcount > 0

    def count(self, table: str, filters: dict) -> int:
        sql, values = _count_sql(table, filters, "%s")
        with self._conn.cursor() as cur:
            cur.execute(sql, values)
            return cur.fetchone()["n"]

    def delete(self, table: str, row_id: str) -> bool:
        sql, values = _delete_sql(table, row_id, "%s")
        with self._conn.cursor() as cur:
            cur.execute(sql, values)
            return cur.rowcount > 0

    def close(self) -> None:
        self._conn.close()


# =============================================================================
# FACTORY
# =============================================================================


def get_learning_store(
    backend: str = "",
    db_path: Path | None = None,
    dsn: str | None = None,
) -> "LearningStore":
    """
    Build a LearningStore from explicit args or environment.

    Env: LEARNING_BACKEND ("sqlite" default | "postgres"),
         LEARNING_POSTGRES_DSN (required for postgres).
    """
    backend = (backend or os.environ.get("LEARNING_BACKEND", "sqlite")).strip().lower()
    if backend == "sqlite":
        return SqliteLearningStore(db_path=db_path or DEFAULT_DB_PATH)
    if backend == "postgres":
        dsn = dsn or os.environ.get("LEARNING_POSTGRES_DSN", "")
        if not dsn:
            raise ValueError(
                "LEARNING_POSTGRES_DSN must be set when LEARNING_BACKEND=postgres"
            )
        return PostgresLearningStore(dsn=dsn)
    raise ValueError(
        f"Unknown learning backend {backend!r} (expected 'sqlite' or 'postgres')"
    )
