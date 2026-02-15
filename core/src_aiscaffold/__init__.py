"""
aiscaffold - AI project scaffold with 2026 best practices.

Provides:
- CLI: `aiscaffold init`, `aiscaffold doctor`, `aiscaffold add`
- TaskTracker: JSON-based task tracking for agent sessions
- ProgressNotes: Append-only session logging
- EvalHarness: Evaluation infrastructure for agent quality
"""

__version__ = "0.1.0"

from aiscaffold.task_tracker import Task, TaskList, TaskPriority, TaskStatus, create_task_list
from aiscaffold.progress_notes import ProgressEntry, ProgressNotesManager
from aiscaffold.eval_harness import EvalHarness, GraderResult, SuiteResult

__all__ = [
    "Task", "TaskList", "TaskPriority", "TaskStatus", "create_task_list",
    "ProgressEntry", "ProgressNotesManager",
    "EvalHarness", "GraderResult", "SuiteResult",
]
