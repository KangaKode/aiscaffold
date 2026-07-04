#!/usr/bin/env python3
"""sessionStart hook: inject lightweight project orientation at session start.

Builds additional_context from whatever is present in the generated project:
a pointer to the docs index, the top of the README, and a summary of recent
subagent activity. Every section is optional -- the hook never fails when
files are absent. Observational only; always exits cleanly.

Guidance verified: 2026-07.
"""
import json
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("CURSOR_PROJECT_DIR", "."))
DOCS_INDEX_PATH = PROJECT_DIR / "docs" / "INDEX.md"
README_PATH = PROJECT_DIR / "README.md"
SUBAGENT_LOG_PATH = PROJECT_DIR / ".cursor" / "hooks" / "state" / "subagent-log.jsonl"

README_HEAD_LINES = 5


def read_docs_pointer() -> str:
    if DOCS_INDEX_PATH.exists():
        return "Documentation index: docs/INDEX.md -- start there for project orientation."
    if (PROJECT_DIR / "docs").is_dir():
        return "Project documentation lives in docs/."
    return ""


def read_readme_head() -> str:
    if not README_PATH.exists():
        return ""
    try:
        lines = README_PATH.read_text().strip().splitlines()
        head = [line for line in lines[:README_HEAD_LINES] if line.strip()]
        if head:
            return "README (top):\n" + "\n".join(head)
    except OSError:
        pass
    return ""


def read_recent_subagent_summary() -> str:
    if not SUBAGENT_LOG_PATH.exists():
        return ""
    try:
        lines = SUBAGENT_LOG_PATH.read_text().strip().splitlines()
        stops = []
        for line in reversed(lines[-50:]):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("event") == "stop":
                stops.append(entry)
                if len(stops) >= 8:
                    break
        if stops:
            parts = [
                f"- {s.get('subagent_type', '?')}: {s.get('status', '?')} "
                f"({s.get('duration_ms', 0)}ms, {s.get('tool_call_count', 0)} tools)"
                for s in stops
            ]
            return "Recent subagent invocations:\n" + "\n".join(parts)
    except OSError:
        pass
    return ""


def main():
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass

    sections = [
        s
        for s in [
            read_docs_pointer(),
            read_readme_head(),
            read_recent_subagent_summary(),
        ]
        if s
    ]

    output = {}
    if sections:
        output["additional_context"] = "\n\n".join(sections)

    json.dump(output, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.stdout.write("{}\n")
