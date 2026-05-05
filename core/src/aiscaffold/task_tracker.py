"""
Task Tracker - JSON-based task tracking for agent sessions.

Provides structured task lists with pass/fail status tracking.
Uses JSON (not Markdown) because agents are less likely to
"creatively edit" JSON files vs prose formats.

Key constraint: Agents can only change the `status` field.
Task descriptions, acceptance criteria, and IDs are immutable
once created.

Trigger: Used by orchestration layer at session start/end.
Output: TaskList with structured pass/fail tracking.
Task Boundary: Tracking only. Does NOT execute tasks.

Reference: docs/REFERENCES.md (Anthropic harness guide -- three-layer external state)
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================


class TaskStatus(Enum):
    """Task completion status."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class TaskPriority(Enum):
    """Task priority levels."""

    P0 = "p0"  # Critical -- must be done first
    P1 = "p1"  # Important -- should be done soon
    P2 = "p2"  # Normal -- do when ready
    P3 = "p3"  # Nice to have -- defer if needed


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass
class Task:
    """A single task with structured tracking."""

    id: str
    description: str
    priority: str = TaskPriority.P2.value
    status: str = TaskStatus.PENDING.value
    acceptance_criteria: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: str | None = None
    notes: str = ""

    def mark_in_progress(self) -> None:
        """Mark task as in progress."""
        self.status = TaskStatus.IN_PROGRESS.value
        self.updated_at = datetime.now().isoformat()
        logger.info(f"[TaskTracker] Task '{self.id}' -> in_progress")

    def mark_completed(self) -> None:
        """Mark task as completed."""
        self.status = TaskStatus.COMPLETED.value
        self.completed_at = datetime.now().isoformat()
        self.updated_at = datetime.now().isoformat()
        logger.info(f"[TaskTracker] Task '{self.id}' -> completed")

    def mark_failed(self, reason: str = "") -> None:
        """Mark task as failed."""
        self.status = TaskStatus.FAILED.value
        self.updated_at = datetime.now().isoformat()
        if reason:
            self.notes = f"FAILED: {reason}"
        logger.warning(f"[TaskTracker] Task '{self.id}' -> failed: {reason}")

    @property
    def is_actionable(self) -> bool:
        """Can this task be worked on?"""
        return self.status in (TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value)


@dataclass
class TaskList:
    """
    A structured task list with JSON persistence.

    Agents can only change task status -- never add, remove,
    or edit task descriptions or acceptance criteria.
    """

    name: str
    tasks: list[Task] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # =========================================================================
    # QUERIES
    # =========================================================================

    @property
    def pending_count(self) -> int:
        """Number of pending tasks."""
        return sum(1 for t in self.tasks if t.status == TaskStatus.PENDING.value)

    @property
    def completed_count(self) -> int:
        """Number of completed tasks."""
        return sum(1 for t in self.tasks if t.status == TaskStatus.COMPLETED.value)

    @property
    def total_count(self) -> int:
        """Total number of tasks."""
        return len(self.tasks)

    @property
    def completion_percent(self) -> int:
        """Percentage of tasks completed."""
        if not self.tasks:
            return 0
        return int((self.completed_count / self.total_count) * 100)

    def get_next_task(self) -> Task | None:
        """
        Get the highest-priority incomplete task.

        Priority order: P0 > P1 > P2 > P3
        Within same priority: first in list wins.
        """
        priority_order = [p.value for p in TaskPriority]
        for priority in priority_order:
            for task in self.tasks:
                if task.is_actionable and task.priority == priority:
                    return task
        return None

    def get_task(self, task_id: str) -> Task | None:
        """Get a task by ID."""
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    # =========================================================================
    # PERSISTENCE (JSON)
    # =========================================================================

    def save(self, filepath: str | Path) -> None:
        """Save task list to JSON file."""
        filepath = Path(filepath)
        data = {
            "name": self.name,
            "created_at": self.created_at,
            "summary": {
                "total": self.total_count,
                "completed": self.completed_count,
                "pending": self.pending_count,
                "completion_percent": self.completion_percent,
            },
            "tasks": [asdict(t) for t in self.tasks],
        }

        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        logger.info(
            f"[TaskTracker] Saved {self.total_count} tasks to {filepath} "
            f"({self.completion_percent}% complete)"
        )

    @classmethod
    def load(cls, filepath: str | Path) -> "TaskList":
        """Load task list from JSON file."""
        filepath = Path(filepath)
        if not filepath.exists():
            logger.warning(f"[TaskTracker] File not found: {filepath}")
            return cls(name="default")

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        tasks = [
            Task(
                id=t["id"],
                description=t["description"],
                priority=t.get("priority", TaskPriority.P2.value),
                status=t.get("status", TaskStatus.PENDING.value),
                acceptance_criteria=t.get("acceptance_criteria", []),
                created_at=t.get("created_at", ""),
                updated_at=t.get("updated_at", ""),
                completed_at=t.get("completed_at"),
                notes=t.get("notes", ""),
            )
            for t in data.get("tasks", [])
        ]

        task_list = cls(
            name=data.get("name", "default"),
            tasks=tasks,
            created_at=data.get("created_at", ""),
        )

        logger.info(
            f"[TaskTracker] Loaded {task_list.total_count} tasks from {filepath} "
            f"({task_list.completion_percent}% complete)"
        )
        return task_list


# =============================================================================
# FACTORY FUNCTIONS
# =============================================================================


def create_task_list(name: str, tasks: list[dict]) -> TaskList:
    """
    Create a new task list from a list of task definitions.

    Args:
        name: Name for the task list
        tasks: List of dicts with keys: id, description, priority, acceptance_criteria

    Returns:
        TaskList instance
    """
    task_objects = [
        Task(
            id=t["id"],
            description=t["description"],
            priority=t.get("priority", TaskPriority.P2.value),
            acceptance_criteria=t.get("acceptance_criteria", []),
        )
        for t in tasks
    ]
    return TaskList(name=name, tasks=task_objects)
