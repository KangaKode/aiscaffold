#!/usr/bin/env python3
"""beforeShellExecution hook: block dangerous git and shell commands.

Blocks: hook bypasses (--no-verify), force push to main/master, recursive rm
on root-ish paths, curl/wget piped to shell, credential-file reads.
Warns (ask): git push, git reset --hard, reading .env files.
Fail-closed: unhandled exceptions exit 2 (deny).

Guidance verified: 2026-07.
"""
import json
import re
import sys

# Read-style commands used in credential-read patterns below.
_READ_CMDS = r"(?:cat|less|more|head|tail|bat|strings|xxd|od|vi|vim|nano|open|source|\.)"

BLOCK_PATTERNS = [
    (
        re.compile(r"git\s+commit\s.*--no-verify|git\s+commit\s.*\s-n\b|git\s+commit\s+-n\b"),
        "This project requires pre-commit hooks and security gates. Use 'git commit' without --no-verify.",
    ),
    (
        re.compile(r"git\s+push\s.*--no-verify"),
        "Push hooks must not be bypassed.",
    ),
    (
        re.compile(
            r"git\s+push\s+.*--force.*\b(main|master)\b"
            r"|git\s+push\s+.*\b(main|master)\b.*--force"
            r"|git\s+push\s+-f\s+.*\b(main|master)\b"
        ),
        "Force push to main/master is destructive and irreversible.",
    ),
    (
        # Covers both -rf and -fr flag orderings; root-ish targets only.
        re.compile(
            r"\brm\s+-[a-zA-Z]*(rf|fr)[a-zA-Z]*\s+"
            r"(/(\s|$)|~/?(\s|$)|\.(\s|$)|/(usr|etc|var|home|bin|sbin|lib|opt|boot)\b)"
        ),
        "Recursive deletion of root, home, system, or current directory is blocked.",
    ),
    (
        re.compile(r"(curl|wget)\s[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b"),
        "Piping remote content to shell is a supply chain attack vector.",
    ),
    (
        re.compile(_READ_CMDS + r"\s+[^|;&]*\.aws/credentials\b"),
        "Reading cloud credential files is blocked.",
    ),
    (
        re.compile(_READ_CMDS + r"\s+[^|;&]*(?:\.ssh/)?id_(rsa|ed25519|ecdsa|dsa)\b(?!\.pub)"),
        "Reading SSH private keys is blocked.",
    ),
    (
        re.compile(_READ_CMDS + r"\s+[^|;&]*(\.netrc|\.pgpass|\.docker/config\.json)\b"),
        "Reading stored credential files is blocked.",
    ),
]

WARN_PATTERNS = [
    (
        re.compile(r"(?:^|[;&|]\s*)git\s+push\b"),
        "Review branch and remote before pushing. Run the test suite first if you haven't.",
    ),
    (
        re.compile(r"git\s+reset\s+--hard"),
        "Hard reset discards uncommitted changes. Verify this is intentional.",
    ),
    (
        re.compile(_READ_CMDS + r"\s+[^|;&]*\.env\b(?!\.(example|template|sample))"),
        "Reading a .env file may expose secrets to the model context. Confirm this is intentional.",
    ),
]


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        json.dump({"permission": "allow"}, sys.stdout)
        return

    command = payload.get("command", "")

    for pattern, message in BLOCK_PATTERNS:
        if pattern.search(command):
            json.dump(
                {
                    "permission": "deny",
                    "user_message": f"[Security Hook] BLOCKED: {message}",
                    "agent_message": f"Command blocked by beforeShellExecution hook. {message}",
                },
                sys.stdout,
            )
            return

    for pattern, message in WARN_PATTERNS:
        if pattern.search(command):
            json.dump(
                {
                    "permission": "ask",
                    "user_message": f"[Security Hook] {message}",
                    "agent_message": f"Shell command flagged by hook. {message} Awaiting user approval.",
                },
                sys.stdout,
            )
            return

    json.dump({"permission": "allow"}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Fail closed: this is a security gate.
        sys.exit(2)
