#!/usr/bin/env python3
"""subagentStart hook: log subagent invocations for observability.

Appends a structured entry to .cursor/hooks/state/subagent-log.jsonl whenever
a subagent is spawned. Always allows -- this is observational only.

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
        json.dump({"permission": "allow"}, sys.stdout)
        return

    entry = {
        "event": "start",
        "timestamp": datetime.now(UTC).isoformat(),
        "subagent_id": payload.get("subagent_id", ""),
        "subagent_type": payload.get("subagent_type", ""),
        "task": payload.get("task", ""),
        "model": payload.get("subagent_model", ""),
        "is_parallel": payload.get("is_parallel_worker", False),
        "conversation_id": payload.get("conversation_id", ""),
    }

    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass

    json.dump({"permission": "allow"}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        json.dump({"permission": "allow"}, sys.stdout)
