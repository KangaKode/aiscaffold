#!/usr/bin/env python3
"""subagentStop hook: log subagent completion with results.

Appends a structured entry to .cursor/hooks/state/subagent-log.jsonl with
summary, duration, tool calls, and modified files. Observational only.

Guidance verified: 2026-07.
"""
import json
import os
import sys
from datetime import datetime, UTC

STATE_DIR = os.path.join(os.environ.get("CURSOR_PROJECT_DIR", "."), ".cursor", "hooks", "state")
LOG_PATH = os.path.join(STATE_DIR, "subagent-log.jsonl")


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.stdout.write("{}\n")
        return

    entry = {
        "event": "stop",
        "timestamp": datetime.now(UTC).isoformat(),
        "subagent_type": payload.get("subagent_type", ""),
        "status": payload.get("status", ""),
        "task": payload.get("task", ""),
        "description": payload.get("description", ""),
        "summary_length": len(payload.get("summary", "")),
        "duration_ms": payload.get("duration_ms", 0),
        "message_count": payload.get("message_count", 0),
        "tool_call_count": payload.get("tool_call_count", 0),
        "modified_files": payload.get("modified_files", []),
        "conversation_id": payload.get("conversation_id", ""),
    }

    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass

    sys.stdout.write("{}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.stdout.write("{}\n")
