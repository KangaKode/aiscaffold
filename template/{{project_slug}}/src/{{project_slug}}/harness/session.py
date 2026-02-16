"""
Session Lifecycle Management - Item/Turn/Thread + Initializer/Worker.

Implements the agent session protocol from 2026 best practices:

  Item  -- Atomic unit of I/O (message, tool call, approval request)
  Turn  -- One unit of agent work initiated by user input
  Thread -- Durable container for an ongoing session

Initializer/Worker Pattern:
  FIRST RUN:  Initialize project, create task list, health check
  EVERY RUN:  Startup ritual -> health check -> work -> cleanup ritual

Human-in-the-loop gates:
  - Health check must pass before work begins
  - Cleanup requires clean state verification
  - Approval requests pause the turn until human responds

Reference: docs/REFERENCES.md

Keep this file under 300 lines.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# PRIMITIVES (OpenAI Item/Turn/Thread model)
# =============================================================================


@dataclass
class Item:
    """Atomic unit of agent I/O."""

    id: str
    type: str  # "message", "tool_call", "approval_request", "result"
    content: str
    status: str = "started"  # started -> delta -> completed
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def complete(self, result: str = "") -> None:
        self.status = "completed"
        if result:
            self.content = result


@dataclass
class Turn:
    """One unit of agent work initiated by user input."""

    id: str
    items: list[Item] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: str | None = None
    requires_approval: bool = False  # Human-in-the-loop gate

    def add_item(self, item: Item) -> None:
        self.items.append(item)

    def complete(self) -> None:
        self.completed_at = datetime.now().isoformat()

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None


@dataclass
class Thread:
    """Durable container for an ongoing session. Can be saved, resumed, forked."""

    id: str
    turns: list[Turn] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = "active"  # active, paused, archived

    def add_turn(self, turn: Turn) -> None:
        self.turns.append(turn)

    def fork(self, new_id: str) -> "Thread":
        """Create a branch from this thread's current state."""
        import copy

        forked = copy.deepcopy(self)
        forked.id = new_id
        forked.metadata["forked_from"] = self.id
        forked.metadata["forked_at"] = datetime.now().isoformat()
        return forked

    def archive(self) -> None:
        self.status = "archived"

    def save(self, path: Path) -> None:
        """Persist thread to JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "id": self.id,
            "turns": [
                {
                    "id": t.id,
                    "items": [{"id": i.id, "type": i.type, "content": i.content,
                               "status": i.status, "timestamp": i.timestamp} for i in t.items],
                    "started_at": t.started_at,
                    "completed_at": t.completed_at,
                }
                for t in self.turns
            ],
            "metadata": self.metadata,
            "created_at": self.created_at,
            "status": self.status,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        logger.debug(f"[Thread] Saved to {path}")

    @classmethod
    def load(cls, path: Path) -> "Thread":
        """Load thread from JSON."""
        with open(path) as f:
            data = json.load(f)
        thread = cls(id=data["id"], metadata=data.get("metadata", {}),
                     created_at=data.get("created_at", ""), status=data.get("status", "active"))
        for t_data in data.get("turns", []):
            turn = Turn(id=t_data["id"], started_at=t_data.get("started_at", ""),
                        completed_at=t_data.get("completed_at"))
            for i_data in t_data.get("items", []):
                turn.add_item(Item(id=i_data["id"], type=i_data["type"],
                                   content=i_data["content"], status=i_data.get("status", "completed"),
                                   timestamp=i_data.get("timestamp", "")))
            thread.add_turn(turn)
        return thread


# =============================================================================
# SESSION PROTOCOL (Initializer/Worker pattern)
# =============================================================================


class SessionProtocol:
    """
    Base protocol for agent session lifecycle.

    Initializer/Worker Pattern:
      FIRST RUN:  self.initialize() -> create task list, project setup
      EVERY RUN:  self.startup() -> self.health_check() -> self.work() -> self.cleanup()

    Subclass and override for your project. The run() method enforces
    the ritual ordering -- work() will NOT execute if health_check() fails.

    Usage:
        class MySession(SessionProtocol):
            async def work(self):
                # Your agent logic here
                pass

        session = MySession(work_dir=Path("."))
        await session.run()
    """

    def __init__(self, work_dir: Path, is_first_run: bool = False):
        self.work_dir = work_dir
        self.is_first_run = is_first_run
        self._thread: Thread | None = None
        self._task_list: Any = None
        self._progress_mgr: Any = None
        self._user_context: str = ""
        self._pending_feedback: list = []
        logger.info(f"[Session] Protocol initialized (first_run={is_first_run})")

    @property
    def thread(self) -> Thread:
        if self._thread is None:
            self._thread = Thread(id=f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        return self._thread

    # =========================================================================
    # OVERRIDE THESE IN YOUR PROJECT
    # =========================================================================

    async def initialize(self) -> None:
        """FIRST RUN ONLY: Set up project structure, create initial task list."""
        logger.info("[Session] Running first-run initialization")

    async def startup(self) -> None:
        """EVERY RUN: Read task list, progress notes, get up to speed.

        Three-layer external state (per 2026 best practices):
          1. Task list (JSON) -- what remains to be done
          2. Progress notes -- what was recently attempted
          3. Git history -- what code changed

        Learning system integration: loads user profile and preferences
        into self._user_context if the learning module is available.
        """
        logger.info("[Session] Running startup ritual")

        try:
            from aiscaffold.task_tracker import TaskList

            task_path = self.work_dir / "tasks.json"
            if task_path.exists():
                self._task_list = TaskList.load(task_path)
                incomplete = [t for t in self._task_list.tasks if t.status.value in ("pending", "in_progress")]
                logger.info(f"[Session] Loaded task list: {len(incomplete)} incomplete tasks")
        except ImportError:
            logger.debug("[Session] aiscaffold core not installed -- task tracking unavailable")
        except Exception as e:
            logger.debug(f"[Session] Task list load failed: {e}")

        try:
            from aiscaffold.progress_notes import ProgressNotesManager

            self._progress_mgr = ProgressNotesManager(db_path=self.work_dir / "data" / "progress.db")
            recent = self._progress_mgr.get_recent(limit=3)
            if recent:
                logger.info(f"[Session] Loaded {len(recent)} recent progress notes")
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"[Session] Progress notes load failed: {e}")

        try:
            from ..learning.user_profile import UserProfileManager

            profile_mgr = UserProfileManager()
            self._user_context = profile_mgr.get_context_bundle()
            if self._user_context:
                logger.info("[Session] Loaded user preferences into context")
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"[Session] Learning system not available: {e}")

    async def health_check(self) -> bool:
        """Verify system is healthy before starting work. Return False to abort."""
        logger.info("[Session] Running health check")
        return True

    async def work(self) -> None:
        """Execute the highest-priority task. THIS IS YOUR MAIN LOGIC."""
        raise NotImplementedError("Override work() with your agent logic")

    async def cleanup(self) -> None:
        """EVERY RUN: Update progress, commit state, leave clean.

        Three-layer state update:
          1. Save task list (if loaded)
          2. Append progress note (if manager available)

        Learning system integration: flushes any pending feedback signals.
        """
        logger.info("[Session] Running cleanup ritual")

        if self._task_list is not None:
            try:
                task_path = self.work_dir / "tasks.json"
                self._task_list.save(task_path)
                logger.info("[Session] Saved task list")
            except Exception as e:
                logger.debug(f"[Session] Task list save failed: {e}")

        if self._progress_mgr is not None:
            try:
                from aiscaffold.progress_notes import ProgressEntry

                self._progress_mgr.append(ProgressEntry(
                    session_id=self.thread.id,
                    completed=[],
                    attempted=[],
                    remaining=[],
                    issues=[],
                    notes="Session cleanup -- update this with actual progress",
                ))
                logger.info("[Session] Appended progress note")
            except Exception as e:
                logger.debug(f"[Session] Progress note append failed: {e}")

        if self._pending_feedback:
            try:
                from ..learning.feedback_tracker import FeedbackTracker

                tracker = FeedbackTracker()
                for signal in self._pending_feedback:
                    tracker.record(signal)
                logger.info(
                    f"[Session] Recorded {len(self._pending_feedback)} "
                    f"feedback signals"
                )
                self._pending_feedback.clear()
            except ImportError:
                pass
            except Exception as e:
                logger.debug(f"[Session] Feedback flush failed: {e}")

    def queue_feedback(self, signal) -> None:
        """Queue a feedback signal to be recorded during cleanup.

        Usage in work():
            from ..learning.models import FeedbackSignal
            self.queue_feedback(FeedbackSignal(
                signal_type="accept", agent_id="analyst", context_type="session"
            ))
        """
        self._pending_feedback.append(signal)

    # =========================================================================
    # DO NOT OVERRIDE -- ENFORCES RITUAL ORDERING
    # =========================================================================

    async def run(self) -> None:
        """Execute the full session lifecycle. Do not override."""
        # First run initialization
        if self.is_first_run:
            await self.initialize()

        # Startup ritual
        await self.startup()

        # Health check gate (human-in-the-loop: won't proceed if unhealthy)
        if not await self.health_check():
            logger.error("[Session] Health check FAILED -- aborting session")
            return

        # Work
        try:
            await self.work()
        except Exception as e:
            logger.error(f"[Session] Work failed: {e}", exc_info=True)
            raise
        finally:
            # Cleanup always runs (even on failure)
            await self.cleanup()

        logger.info("[Session] Session complete")
