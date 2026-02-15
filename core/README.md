# aiscaffold

AI project scaffold with 2026 best practices -- CLI and core utilities.

## Install

```bash
pip install aiscaffold
```

## CLI Usage

```bash
# Create a new project
aiscaffold init my-project

# Check project health
aiscaffold doctor

# Add optional modules
aiscaffold add evals        # Eval infrastructure
aiscaffold add state        # Task tracker + progress notes
aiscaffold add agent:my-bot # New subagent
aiscaffold add layer:api    # New architecture layer

# Pull template updates
aiscaffold update
```

## Core Utilities

```python
from aiscaffold import TaskList, create_task_list, ProgressNotesManager
from aiscaffold import EvalHarness, GraderResult, SuiteResult

# Task tracking (JSON-based, not Markdown)
tasks = create_task_list("sprint-1", [
    {"id": "feat-1", "description": "Build user auth", "priority": "p0"},
    {"id": "feat-2", "description": "Add dashboard", "priority": "p1"},
])
tasks.save("tasks.json")

# Eval harness
result = GraderResult(eval_name="voice_match", passed=True, score=0.92)
suite = SuiteResult(suite_name="regression", results=[result])
harness = EvalHarness()
harness.save_results(suite)
```
