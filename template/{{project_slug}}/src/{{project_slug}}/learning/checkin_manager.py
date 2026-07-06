"""
CheckInManager -- Permission-based adaptation gates.

The system NEVER adapts silently. When it detects a pattern worth acting on
(e.g., user consistently rejects a certain style), it creates a CheckIn
and waits for the user to approve before changing behavior.

Trigger types:
  - "threshold": N signals of the same type reached
  - "time": Periodic check-in (every N hours/sessions)
  - "drift": A preference value changed significantly
  - "milestone": First N interactions with a new agent

Lifecycle: PENDING -> APPROVED / REJECTED / EXPIRED / SKIPPED

Security:
  - Check-in prompts are sanitized before storage
  - Expired check-ins are auto-cleaned (status flip only -- rows and
    their prompt text persist until erase_for_correction removes them)

Keep this file under 350 lines. (Raised from 300: the GDPR erasure hook
(erase_for_correction) landed here because correction check-ins embed
the proposed text verbatim; split query helpers out if it grows again.)
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from ..security.prompt_guard import sanitize_for_prompt
from .models import CheckIn, CheckInStatus
from .schema import DEFAULT_DB_PATH, dict_from_row, get_connection, initialize_schema

logger = logging.getLogger(__name__)

DEFAULT_EXPIRY_HOURS = 72
DEFAULT_THRESHOLD = 10


class CheckInManager:
    """
    Manages the check-in lifecycle for permission-based adaptation.

    Usage:
        mgr = CheckInManager()

        # Check if a check-in should trigger
        if mgr.should_trigger("threshold", agent_id="analyst", signal_count=15):
            checkin = mgr.create(
                checkin_type="threshold",
                prompt="You've accepted 15 suggestions from the analyst. Trust this agent more?",
                suggested_action="Increase analyst trust score to 0.8",
            )

        # User responds
        mgr.respond(checkin.id, approved=True, response="Yes, increase trust")

        # Query pending check-ins
        pending = mgr.get_pending()
    """

    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        default_expiry_hours: int = DEFAULT_EXPIRY_HOURS,
    ):
        self._db_path = db_path
        self._default_expiry_hours = default_expiry_hours
        initialize_schema(db_path)

    def create(
        self,
        checkin_type: str,
        prompt: str,
        suggested_action: str = "",
        project_id: str = "default",
        context: dict | None = None,
        expiry_hours: int | None = None,
    ) -> CheckIn:
        """Create a new check-in and persist it."""
        prompt = sanitize_for_prompt(prompt, max_length=5000)
        suggested_action = sanitize_for_prompt(suggested_action, max_length=2000)

        hours = expiry_hours or self._default_expiry_hours
        expires_at = (datetime.now() + timedelta(hours=hours)).isoformat()

        checkin = CheckIn(
            checkin_type=checkin_type,
            prompt=prompt,
            suggested_action=suggested_action,
            project_id=project_id,
            context=context or {},
            expires_at=expires_at,
        )

        conn = get_connection(self._db_path)
        try:
            conn.execute(
                """INSERT INTO checkins
                   (id, project_id, checkin_type, prompt, suggested_action,
                    status, response, context_json, created_at, expires_at, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkin.id,
                    checkin.project_id,
                    checkin.checkin_type,
                    checkin.prompt,
                    checkin.suggested_action,
                    checkin.status,
                    checkin.response,
                    json.dumps(checkin.context, default=str),
                    checkin.created_at,
                    checkin.expires_at,
                    checkin.resolved_at,
                ),
            )
            conn.commit()
            logger.info(
                f"[CheckIn] Created {checkin.checkin_type} check-in: {checkin.id}"
            )
            return checkin
        finally:
            conn.close()

    def respond(
        self,
        checkin_id: str,
        approved: bool,
        response: str = "",
    ) -> CheckIn | None:
        """Record the user's response to a check-in."""
        status = CheckInStatus.APPROVED if approved else CheckInStatus.REJECTED
        resolved_at = datetime.now().isoformat()
        response = sanitize_for_prompt(response, max_length=2000)

        conn = get_connection(self._db_path)
        try:
            conn.execute(
                """UPDATE checkins SET status = ?, response = ?, resolved_at = ?
                   WHERE id = ? AND status = ?""",
                (status, response, resolved_at, checkin_id, CheckInStatus.PENDING),
            )
            conn.commit()

            if conn.total_changes == 0:
                logger.warning(f"[CheckIn] {checkin_id} not found or already resolved")
                return None

            logger.info(f"[CheckIn] {checkin_id} -> {status}")
            return self._get_by_id(checkin_id, conn)
        finally:
            conn.close()

    def skip(self, checkin_id: str) -> bool:
        """Skip a check-in (user doesn't want to decide now)."""
        conn = get_connection(self._db_path)
        try:
            conn.execute(
                "UPDATE checkins SET status = ? WHERE id = ? AND status = ?",
                (CheckInStatus.SKIPPED, checkin_id, CheckInStatus.PENDING),
            )
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def get(self, checkin_id: str) -> CheckIn | None:
        """Get a check-in by ID (any status)."""
        conn = get_connection(self._db_path)
        try:
            return self._get_by_id(checkin_id, conn)
        finally:
            conn.close()

    def get_pending(self, project_id: str = "default") -> list[CheckIn]:
        """Get all pending (unresolved, non-expired) check-ins."""
        self._expire_old(project_id)
        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(
                """SELECT * FROM checkins
                   WHERE project_id = ? AND status = ?
                   ORDER BY created_at ASC""",
                (project_id, CheckInStatus.PENDING),
            ).fetchall()
            return [self._row_to_checkin(dict_from_row(r)) for r in rows]
        finally:
            conn.close()

    def should_trigger(
        self,
        trigger_type: str,
        project_id: str = "default",
        signal_count: int = 0,
        threshold: int = DEFAULT_THRESHOLD,
        **kwargs,
    ) -> bool:
        """
        Check if a check-in should be triggered.

        For "threshold": triggers when signal_count >= threshold AND
        there's no pending check-in of the same type.
        """
        if trigger_type == "threshold" and signal_count >= threshold:
            existing = self._has_pending_of_type(trigger_type, project_id)
            return not existing

        if trigger_type == "milestone" and signal_count in (1, 5, 10, 25, 50, 100):
            existing = self._has_pending_of_type(trigger_type, project_id)
            return not existing

        return False

    def _expire_old(self, project_id: str) -> int:
        """Mark expired check-ins."""
        now = datetime.now().isoformat()
        conn = get_connection(self._db_path)
        try:
            conn.execute(
                """UPDATE checkins SET status = ?
                   WHERE project_id = ? AND status = ?
                   AND expires_at != '' AND expires_at < ?""",
                (CheckInStatus.EXPIRED, project_id, CheckInStatus.PENDING, now),
            )
            conn.commit()
            expired = conn.total_changes
            if expired > 0:
                logger.debug(f"[CheckIn] Expired {expired} check-ins")
            return expired
        finally:
            conn.close()

    def erase_for_correction(self, correction_id: str) -> int:
        """Hard-delete every check-in created for a correction (any status).

        Correction proposals create a check-in whose prompt embeds the
        original/corrected claim text verbatim, so a GDPR erasure of the
        correction (learning/erasure.py) must remove those rows too --
        _expire_old only flips status and never deletes. Matching is on
        the stored context citing the correction id: a LIKE prefilter
        narrows candidates, then the JSON is parsed to confirm, so a
        substring hit inside some other value can never delete an
        unrelated row. Returns the number of rows deleted.
        """
        if not correction_id:
            return 0
        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(
                "SELECT id, context_json FROM checkins WHERE context_json LIKE ?",
                (f"%{correction_id}%",),
            ).fetchall()
            doomed = []
            for row in rows:
                try:
                    context = json.loads(row["context_json"] or "{}")
                except (ValueError, TypeError):
                    continue
                if context.get("correction_id") == correction_id:
                    doomed.append(row["id"])
            for checkin_id in doomed:
                conn.execute("DELETE FROM checkins WHERE id = ?", (checkin_id,))
            conn.commit()
            if doomed:
                logger.info(
                    f"[CheckIn] Erased {len(doomed)} check-in(s) for "
                    f"correction {correction_id}"
                )
            return len(doomed)
        finally:
            conn.close()

    def _has_pending_of_type(self, checkin_type: str, project_id: str) -> bool:
        """Check if there's already a pending check-in of this type."""
        conn = get_connection(self._db_path)
        try:
            row = conn.execute(
                """SELECT COUNT(*) as count FROM checkins
                   WHERE project_id = ? AND checkin_type = ? AND status = ?""",
                (project_id, checkin_type, CheckInStatus.PENDING),
            ).fetchone()
            return (row["count"] if row else 0) > 0
        finally:
            conn.close()

    def _get_by_id(self, checkin_id: str, conn) -> CheckIn | None:
        """Get a check-in by ID using an existing connection."""
        row = conn.execute(
            "SELECT * FROM checkins WHERE id = ?", (checkin_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_checkin(dict_from_row(row))

    @staticmethod
    def _row_to_checkin(data: dict) -> CheckIn:
        """Convert a database row dict to a CheckIn."""
        # Context is persisted in the context_json column; parse it back so
        # consumers (e.g. graduation apply) see what create() stored.
        context = data.get("context") or {}
        if not context:
            raw = data.get("context_json", "")
            if raw:
                try:
                    context = json.loads(raw)
                except (ValueError, TypeError):
                    context = {}
        return CheckIn(
            id=data["id"],
            project_id=data.get("project_id", "default"),
            checkin_type=data["checkin_type"],
            prompt=data["prompt"],
            suggested_action=data.get("suggested_action", ""),
            status=data.get("status", CheckInStatus.PENDING),
            response=data.get("response", ""),
            context=context,
            created_at=data.get("created_at", ""),
            expires_at=data.get("expires_at", ""),
            resolved_at=data.get("resolved_at", ""),
        )
