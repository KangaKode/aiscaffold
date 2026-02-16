"""
GlobalProfileManager -- Cross-project user identity.

Stores interaction metadata that applies across all projects scaffolded
with aiscaffold. Located at ~/.aiscaffold/global_profile.db.

Tracks:
  - Interaction style (verbosity, formality, detail level)
  - Preferred agents (graduated from project-level trust)
  - Global preferences (graduated from project-level preferences)

No domain-specific fields -- just vanilla interaction metadata.

Keep this file under 200 lines.
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from ..security.prompt_guard import sanitize_for_prompt

logger = logging.getLogger(__name__)

GLOBAL_DB_PATH = Path.home() / ".aiscaffold" / "global_profile.db"

GLOBAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS global_preferences (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    source_project TEXT DEFAULT '',
    graduated_at TEXT DEFAULT '',
    confidence REAL DEFAULT 0.5,
    metadata_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS interaction_style (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_history (
    project_id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_active TEXT NOT NULL,
    total_interactions INTEGER DEFAULT 0,
    metadata_json TEXT DEFAULT '{}'
);
"""


class GlobalProfileManager:
    """
    Manages cross-project user identity.

    Usage:
        global_mgr = GlobalProfileManager()

        # Set interaction style
        global_mgr.set_style("verbosity", "concise")
        global_mgr.set_style("formality", "casual")

        # Graduate a preference from a project
        global_mgr.add_global_preference(
            key="prefer_evidence",
            value="Always cite sources",
            source_project="my_project",
        )

        # Get all global preferences (for seeding new projects)
        prefs = global_mgr.get_global_preferences()

        # Record project activity
        global_mgr.record_project_activity("my_project", interactions=5)
    """

    def __init__(self, db_path: Path = GLOBAL_DB_PATH):
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Create global profile database if it doesn't exist."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(GLOBAL_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def set_style(self, key: str, value: str) -> None:
        """Set an interaction style preference (e.g., verbosity, formality)."""
        key = sanitize_for_prompt(key, max_length=200)
        value = sanitize_for_prompt(value, max_length=2000)
        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO interaction_style (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at""",
                (key, value, datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_style(self, key: str, default: str = "") -> str:
        """Get an interaction style value."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT value FROM interaction_style WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default
        finally:
            conn.close()

    def get_all_styles(self) -> dict[str, str]:
        """Get all interaction style preferences."""
        conn = self._conn()
        try:
            rows = conn.execute("SELECT key, value FROM interaction_style").fetchall()
            return {r["key"]: r["value"] for r in rows}
        finally:
            conn.close()

    def add_global_preference(
        self,
        key: str,
        value: str,
        source_project: str = "",
        confidence: float = 0.5,
        pref_id: str = "",
    ) -> None:
        """Add or update a global preference (graduated from a project)."""
        import uuid

        key = sanitize_for_prompt(key, max_length=500)
        value = sanitize_for_prompt(value, max_length=5000)
        pref_id = pref_id or str(uuid.uuid4())[:12]
        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO global_preferences
                   (id, key, value, source_project, graduated_at, confidence, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, '{}')
                   ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    confidence = excluded.confidence,
                    graduated_at = excluded.graduated_at""",
                (pref_id, key, value, source_project, datetime.now().isoformat(), confidence),
            )
            conn.commit()
            logger.info(f"[GlobalProfile] Added preference: {key}={value}")
        finally:
            conn.close()

    def get_global_preferences(self) -> list[dict[str, Any]]:
        """Get all global preferences (for seeding new projects)."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM global_preferences ORDER BY confidence DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def record_project_activity(
        self, project_id: str, interactions: int = 1
    ) -> None:
        """Record activity for a project."""
        now = datetime.now().isoformat()
        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO project_history
                   (project_id, first_seen, last_active, total_interactions, metadata_json)
                   VALUES (?, ?, ?, ?, '{}')
                   ON CONFLICT(project_id) DO UPDATE SET
                    last_active = excluded.last_active,
                    total_interactions = total_interactions + excluded.total_interactions""",
                (project_id, now, now, interactions),
            )
            conn.commit()
        finally:
            conn.close()

    def get_project_history(self) -> list[dict[str, Any]]:
        """Get all known projects and their activity."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM project_history ORDER BY last_active DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
