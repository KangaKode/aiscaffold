#!/usr/bin/env python3
"""beforeShellExecution: require Bugbot + Security Review receipt before git commit.

Defense-in-depth for Cursor shell `git commit` only — not fail-closed integrity.
Fail-closed on ambiguous compound commands. Bypass: ROUNDTABLE_SKIP_REVIEW_RECEIPT=1.

No hooks.json matcher: script early-allows non-commits so `git -C` / `git -c`
forms are not bypassed by a brittle `git\\s+commit` matcher.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HOOK_REPO = Path(__file__).resolve().parents[2]
_COMPOUND_TOKENS = frozenset({"&&", "||", ";"})


def _git_common_dir(repo: Path) -> Path | None:
    """Shared git dir (main + linked worktrees); None if not a checkout."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(repo),
            text=True,
        ).strip()
        path = Path(out)
        if not path.is_absolute():
            path = (repo / path).resolve()
        else:
            path = path.resolve()
        return path
    except (subprocess.CalledProcessError, OSError):
        return None


def _tokenize(command: str) -> list[str] | None:
    """Shell-ish tokens; None if quotes are unbalanced (treat as ambiguous)."""
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return None


def _is_git_commit(tokens: list[str]) -> bool:
    """True if tokens start with git and the subcommand is commit."""
    if not tokens or tokens[0] != "git":
        return False
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t == "commit":
            return True
        if t in {"-C", "-c"}:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return False
    return False


def _has_git_commit(tokens: list[str]) -> bool:
    return any(
        _is_git_commit(tokens[i:]) for i, t in enumerate(tokens) if t == "git"
    )


def _resolve_target(git_tokens: list[str], cwd: str) -> Path | None | str:
    """Path, or \"unsupported\", or None if toplevel cannot be resolved."""
    for t in git_tokens:
        if t.startswith("--git-dir") or t.startswith("--work-tree"):
            return "unsupported"
        if t.startswith("-C") and t != "-C":
            return "unsupported"

    i = 1  # skip leading `git`
    while i < len(git_tokens):
        t = git_tokens[i]
        if t == "commit":
            break
        if t == "-C":
            if i + 1 >= len(git_tokens):
                return None
            path = Path(git_tokens[i + 1])
            if not path.is_absolute():
                path = Path(cwd) / path
            try:
                return path.resolve()
            except OSError:
                return None
        if t == "-c":
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return None
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            text=True,
        ).strip()
        return Path(out).resolve()
    except (subprocess.CalledProcessError, OSError):
        return None


def _mask_quoted_and_heredocs(command: str) -> str:
    """Replace quoted spans and heredoc bodies with spaces (keep structure).

    Used so substitution checks ignore commit-message / PR-body text while
    still catching `git $(echo commit)` outside quotes.
    """
    out: list[str] = []
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch in "'\"":
            quote = ch
            out.append(" ")
            i += 1
            while i < n and command[i] != quote:
                if command[i] == "\\" and quote == '"' and i + 1 < n:
                    i += 2
                    continue
                i += 1
            if i < n:
                i += 1
            out.append(" ")
            continue
        if command.startswith("<<", i):
            out.append("  ")
            i += 2
            while i < n and command[i] in "-":
                out.append(" ")
                i += 1
            quote = ""
            if i < n and command[i] in "'\"":
                quote = command[i]
                out.append(" ")
                i += 1
            tag_chars: list[str] = []
            while i < n and command[i] not in " \t\n":
                if quote and command[i] == quote:
                    i += 1
                    break
                tag_chars.append(command[i])
                out.append(" ")
                i += 1
            tag = "".join(tag_chars)
            # Consume through newline after <<TAG, then body until tag line.
            while i < n and command[i] != "\n":
                out.append(" ")
                i += 1
            if i < n:
                out.append("\n")
                i += 1
            while i < n:
                line_start = i
                while i < n and command[i] != "\n":
                    i += 1
                line = command[line_start:i]
                if line.strip() == tag:
                    out.append(" " * (i - line_start))
                    if i < n:
                        out.append("\n")
                        i += 1
                    break
                out.append(" " * (i - line_start))
                if i < n:
                    out.append("\n")
                    i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def analyze_command(command: str, cwd: str) -> tuple[str, Path | None]:
    """Classify a shell command for the review gate.

    Returns (action, target) where action is:
      allow | check | ambiguous | unsupported | unknown
    """
    masked = _mask_quoted_and_heredocs(command)

    # Unquoted substitution or newlines with `git` → fail closed
    # (e.g. git $(echo commit)). Quoted -m / PR bodies are masked away.
    if re.search(r"\bgit\b", masked) and (
        "`" in masked or "$(" in masked or "\n" in masked
    ):
        return "ambiguous", None

    tokens = _tokenize(command)
    if tokens is None:
        return ("ambiguous", None) if "commit" in command else ("allow", None)

    # Env overrides that redirect git away from argv-resolved cwd (T6 class).
    for t in tokens:
        if t.startswith("GIT_DIR=") or t.startswith("GIT_WORK_TREE="):
            return "unsupported", None
    if tokens and tokens[0] == "env":
        for t in tokens[1:]:
            if t.startswith("GIT_DIR=") or t.startswith("GIT_WORK_TREE="):
                return "unsupported", None
            if t == "git" or not ("=" in t or t.startswith("-")):
                break

    if any(t in _COMPOUND_TOKENS for t in tokens):
        return ("ambiguous", None) if _has_git_commit(tokens) else ("allow", None)

    git_starts = [i for i, t in enumerate(tokens) if t == "git"]
    if len(git_starts) > 1:
        return ("ambiguous", None) if _has_git_commit(tokens) else ("allow", None)
    if not git_starts:
        return "allow", None

    git_tokens = tokens[git_starts[0] :]
    if not _is_git_commit(git_tokens):
        return "allow", None

    target = _resolve_target(git_tokens, cwd)
    if target == "unsupported":
        return "unsupported", None
    if target is None:
        return "unknown", None
    return "check", target


def _deny(user_message: str, agent_message: str) -> None:
    json.dump(
        {
            "permission": "deny",
            "user_message": user_message,
            "agent_message": agent_message,
        },
        sys.stdout,
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        _deny(
            "[Review Gate] BLOCKED: invalid hook input.",
            "Commit blocked: beforeShellExecution payload was not valid JSON.",
        )
        return

    command = payload.get("command", "")
    cwd = payload.get("cwd") or os.getcwd()
    action, target = analyze_command(command, cwd)

    if action == "allow":
        json.dump({"permission": "allow"}, sys.stdout)
        return
    if action == "unsupported":
        _deny(
            "[Review Gate] BLOCKED: use plain `git commit` or "
            "`git -C <path> commit` (spaced); --git-dir/--work-tree "
            "forms are not supported by the review gate.",
            "Commit blocked: unsupported git commit invocation.",
        )
        return
    if action == "ambiguous":
        _deny(
            "[Review Gate] BLOCKED: compound or multi-git shell commands "
            "are not allowed (receipt would be checked before mutations). "
            "Run a single `git commit` after staging.",
            "Commit blocked: ambiguous/compound git commit. Use a lone "
            "`git commit` after `git add`.",
        )
        return
    if action == "unknown" or target is None:
        _deny(
            "[Review Gate] BLOCKED: could not resolve commit target repo.",
            "Commit blocked: unable to resolve git toplevel for receipt check.",
        )
        return

    if os.environ.get("ROUNDTABLE_SKIP_REVIEW_RECEIPT", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        json.dump({"permission": "allow"}, sys.stdout)
        return

    hook_common = _git_common_dir(HOOK_REPO.resolve())
    target_common = _git_common_dir(target)
    if hook_common is None or target_common is None:
        _deny(
            "[Review Gate] BLOCKED: could not resolve git-common-dir.",
            "Commit blocked: git-common-dir resolution failed.",
        )
        return
    if target_common != hook_common:
        json.dump({"permission": "allow"}, sys.stdout)
        return

    check_cwd = target
    script = HOOK_REPO.resolve() / "scripts" / "record_review_receipt.py"
    if not script.is_file():
        script = check_cwd / "scripts" / "record_review_receipt.py"
    if not script.is_file():
        _deny(
            "[Review Gate] BLOCKED: scripts/record_review_receipt.py not found.",
            "Commit blocked. Record Bugbot + Security Review receipts first.",
        )
        return

    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--check"],
            cwd=str(check_cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        _deny(
            f"[Review Gate] BLOCKED: receipt check failed ({exc}).",
            "Commit blocked; could not verify review receipt.",
        )
        return

    if proc.returncode == 0:
        json.dump({"permission": "allow"}, sys.stdout)
        return

    detail = (proc.stderr or proc.stdout or "").strip()
    _deny(
        "[Review Gate] BLOCKED: Bugbot + Security Review receipt missing "
        f"or stale. {detail}",
        "Commit blocked. Run review-bugbot and review-security, then "
        "`python3 scripts/record_review_receipt.py --reviewer …`. "
        "Do not use --no-verify.",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(2)
