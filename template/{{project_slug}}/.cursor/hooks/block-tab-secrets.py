#!/usr/bin/env python3
"""beforeTabFileRead hook: block Tab autocomplete from ingesting secret files.

Checks file_path against compiled sensitive patterns. Keeps .env.example safe.
Must be fast (<10ms) -- uses pre-compiled regexes, no file I/O. The patterns
mirror .cursor/hooks/sensitive-patterns.json; keep both in sync.
Fail-closed: unhandled exceptions exit 2 (deny).

Guidance verified: 2026-07.
"""
import json
import re
import sys

SAFE_SUFFIXES = (".env.example", ".env.template", ".env.sample")

SENSITIVE_RE = [
    re.compile(r"\.env$"),
    re.compile(r"\.env\.\w+$"),
    re.compile(r"\.key$"),
    re.compile(r"\.pem$"),
    re.compile(r"\.p12$"),
    re.compile(r"\.pfx$"),
    re.compile(r"\.jks$"),
    re.compile(r"\.keystore$"),
    re.compile(r"\.secret$"),
    re.compile(r"credentials\.json$"),
    re.compile(r"service[_-]account.*\.json$"),
    re.compile(r"secret\.yaml$"),
    re.compile(r"id_rsa$"),
    re.compile(r"id_ed25519$"),
    re.compile(r"id_ecdsa$"),
    re.compile(r"\.ssh/config$"),
    re.compile(r"\.aws/credentials$"),
    re.compile(r"\.netrc$"),
    re.compile(r"\.pgpass$"),
]


def is_sensitive(file_path: str) -> bool:
    lower = file_path.lower()
    for safe in SAFE_SUFFIXES:
        if lower.endswith(safe):
            return False
    for pattern in SENSITIVE_RE:
        if pattern.search(lower):
            return True
    return False


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        json.dump({"permission": "allow"}, sys.stdout)
        return

    file_path = payload.get("file_path", "")

    if is_sensitive(file_path):
        json.dump({"permission": "deny"}, sys.stdout)
        return

    json.dump({"permission": "allow"}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Fail closed: this is a security gate.
        sys.exit(2)
