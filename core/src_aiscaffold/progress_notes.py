"""
Progress Notes - Append-only session log for agent handoffs.

Each agent session appends what was done, what was attempted,
and what remains. This bridges context windows across sessions
so the next agent can quickly get up to speed.

Three layers of external state (per Anthropic best practices):
1. Task List (JSON) -- what remains to be done
2. Progress Notes (this) -- what was recently attempted
3. Git History -- exactly what code changed

Trigger: Used by orchestration layer at session start/end.
Output: Append-only log entries in SQLite.
Task Boundary: Logging only. Does NOT make decisions.

Reference: docs/AI_ENGINEERING_BEST_PRACTICES_2026.md (Part 3)
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass
class ProgressEntry:
    """A single progress note entry."""

    session_id: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    completed: list[str] = field(default_factory=list)  # What was done
    attempted: list[str] = field(default_factory=list)  # What was tried
    remaining: list[str] = field(default_factory=list)  # What's left
    issues: list[str] = field(default_factory=list)  # Problems encountered
    notes: str = ""  # Free-form notes

    def to_summary(self) -> str:
        """Generate human-readable summary."""
        lines = [f"## Session: {self.session_id}", f"**Time:** {self.timestamp}", ""]

        if self.completed:
            lines.append("**Completed:**")
            lines.extend(f"- {item}" for item in self.completed)
            lines.append("")

        if self.attempted:
            lines.append("**Attempted:**")
            lines.extend(f"- {item}" for item in self.attempted)
            lines.append("")

        if self.remaining:
            lines.append("**Remaining:**")
            lines.extend(f"- {item}" for item in self.remaining)
            lines.append("")

        if self.issues:
            lines.append("**Issues:**")
            lines.extend(f"- {item}" for item in self.issues)
            lines.append("")

        if self.notes:
            lines.append(f"**Notes:** {self.notes}")
            lines.append("")

        return "\n".join(lines)


# =============================================================================
# PROGRESS NOTES MANAGER
# =============================================================================


class ProgressNotesManager:
    """
    Manages append-only progress notes in SQLite.

    Usage:
        manager = ProgressNotesManager(db)
        manager.ensure_table()

        # At end of session
        manager.append(ProgressEntry(
            session_id="session_123",
            completed=["Fixed beat detector tests"],
            remaining=["Update voice verification"],
        ))

        # At start of next session
        recent = manager.get_recent(limit=3)
        for entry in recent:
            print(entry.to_summary())
    """

    def __init__(self, db=None):
        """
        Initialize progress notes manager.

        Args:
            db: Database instance (lazy-loaded if not provided)
        """
        self._db = db
        logger.info("[ProgressNotes] Manager initialized")

    def _get_db(self):
        """Lazy-load database."""
        if self._db is None:
            from data.database import get_database

            self._db = get_database()
        return self._db

    def ensure_table(self) -> None:
        """Create the progress_notes table if it doesn't exist."""
        db = self._get_db()
        try:
            with db._get_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS progress_notes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        completed_json TEXT DEFAULT '[]',
                        attempted_json TEXT DEFAULT '[]',
                        remaining_json TEXT DEFAULT '[]',
                        issues_json TEXT DEFAULT '[]',
                        notes TEXT DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_progress_notes_session ON progress_notes(session_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_progress_notes_timestamp ON progress_notes(timestamp DESC)"
                )
            logger.debug("[ProgressNotes] Table ensured")
        except Exception as e:
            logger.error(f"[ProgressNotes] Table creation failed: {e}", exc_info=True)

    def append(self, entry: ProgressEntry) -> None:
        """
        Append a progress entry (append-only, never update).

        Args:
            entry: Progress entry to store
        """
        db = self._get_db()
        try:
            with db._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO progress_notes
                    (session_id, timestamp, completed_json, attempted_json,
                     remaining_json, issues_json, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.session_id,
                        entry.timestamp,
                        json.dumps(entry.completed),
                        json.dumps(entry.attempted),
                        json.dumps(entry.remaining),
                        json.dumps(entry.issues),
                        entry.notes,
                    ),
                )
            logger.info(
                f"[ProgressNotes] Appended entry for session {entry.session_id}: "
                f"{len(entry.completed)} completed, {len(entry.remaining)} remaining"
            )
        except Exception as e:
            logger.error(f"[ProgressNotes] Append failed: {e}", exc_info=True)

    def get_recent(self, limit: int = 5) -> list[ProgressEntry]:
        """
        Get the most recent progress entries.

        Args:
            limit: Maximum number of entries to return

        Returns:
            List of ProgressEntry, most recent first
        """
        db = self._get_db()
        try:
            with db._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT session_id, timestamp, completed_json, attempted_json,
                           remaining_json, issues_json, notes
                    FROM progress_notes
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                entries = []
                for row in cursor.fetchall():
                    entries.append(
                        ProgressEntry(
                            session_id=row[0],
                            timestamp=row[1],
                            completed=json.loads(row[2]) if row[2] else [],
                            attempted=json.loads(row[3]) if row[3] else [],
                            remaining=json.loads(row[4]) if row[4] else [],
                            issues=json.loads(row[5]) if row[5] else [],
                            notes=row[6] or "",
                        )
                    )
                logger.debug(f"[ProgressNotes] Retrieved {len(entries)} recent entries")
                return entries
        except Exception as e:
            logger.warning(f"[ProgressNotes] Get recent failed: {e}", exc_info=True)
            return []

    def get_summary(self, limit: int = 3) -> str:
        """
        Get a formatted summary of recent progress for agent context.

        Args:
            limit: Number of recent entries to include

        Returns:
            Formatted markdown summary
        """
        entries = self.get_recent(limit=limit)
        if not entries:
            return "No previous session notes found."

        lines = ["# Recent Session Progress", ""]
        for entry in entries:
            lines.append(entry.to_summary())
            lines.append("---")

        return "\n".join(lines)
