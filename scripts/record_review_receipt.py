#!/usr/bin/env python3
"""Record or verify pre-commit Bugbot + Security Review receipts.

Usage:
  python3 scripts/record_review_receipt.py --reviewer bugbot [--findings N]
  python3 scripts/record_review_receipt.py --reviewer security-review [--findings N]
  python3 scripts/record_review_receipt.py --check   # exit 0 if receipt matches tree

Receipt path: .cursor/review-receipts/pre-commit.json (gitignored).

Repo root is always `git rev-parse --show-toplevel` from the process cwd
(so linked worktrees fingerprint the tree being committed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REQUIRED = ("bugbot", "security-review")


def repo_root() -> Path:
    out = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        text=True,
    ).strip()
    return Path(out)


def receipt_path(root: Path) -> Path:
    return root / ".cursor" / "review-receipts" / "pre-commit.json"


def _paths_vs_head(root: Path) -> list[str]:
    """Paths that differ from HEAD or are untracked (sorted, unique)."""
    names = subprocess.check_output(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=root,
        text=True,
    ).splitlines()
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "-uall"],
        cwd=root,
        text=True,
    )
    for line in status.splitlines():
        if line.startswith("?? "):
            names.append(line[3:].strip())
    return sorted(set(n for n in names if n))


def tree_fingerprint(root: Path) -> str:
    """Hash of path → worktree bytes for every path differing from HEAD.

    Staging alone (`git add` with no content change) does not change this
    hash: new files contribute the same path+bytes whether still untracked
    or already in the index. At --check we also require no unstaged tracked
    diffs so the index matches the reviewed worktree before commit.
    """
    h = hashlib.sha256()
    for rel in _paths_vs_head(root):
        h.update(b"P:")
        h.update(rel.encode("utf-8", errors="replace"))
        h.update(b"\0")
        path = root / rel
        if path.is_file():
            h.update(b"F:")
            h.update(path.read_bytes())
        else:
            # Deleted tracked path, or non-file (e.g. directory placeholder).
            h.update(b"D:")
        h.update(b"\0")
    return h.hexdigest()


def load_receipt(root: Path) -> dict:
    path = receipt_path(root)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_receipt(root: Path, data: dict) -> None:
    path = receipt_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def record(reviewer: str, findings: int) -> None:
    if reviewer not in REQUIRED:
        raise SystemExit(f"reviewer must be one of {REQUIRED}")
    root = repo_root()
    fp = tree_fingerprint(root)
    data = load_receipt(root)
    if data.get("tree_fingerprint") != fp:
        data = {"tree_fingerprint": fp, "reviews": {}}
    data["tree_fingerprint"] = fp
    data.setdefault("reviews", {})[reviewer] = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "finding_count": findings,
    }
    save_receipt(root, data)
    print(f"Recorded {reviewer} for fingerprint {fp[:12]}…")


def check() -> int:
    root = repo_root()
    unstaged = subprocess.check_output(
        ["git", "diff", "--name-only"], cwd=root
    ).strip()
    if unstaged:
        print(
            "Unstaged tracked changes remain; git add so the index matches "
            "the reviewed worktree, then re-record reviews if the tree changed.",
            file=sys.stderr,
        )
        return 1
    fp = tree_fingerprint(root)
    data = load_receipt(root)
    if data.get("tree_fingerprint") != fp:
        print(
            "Review receipt missing or stale for current tree. "
            "Run Bugbot and Security Review, then "
            "`python3 scripts/record_review_receipt.py --reviewer …`.",
            file=sys.stderr,
        )
        return 1
    reviews = data.get("reviews") or {}
    missing = [r for r in REQUIRED if r not in reviews]
    if missing:
        print(f"Missing reviews: {', '.join(missing)}", file=sys.stderr)
        return 1
    print("Pre-commit review receipt OK.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewer", choices=REQUIRED)
    parser.add_argument("--findings", type=int, default=0)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    if not args.reviewer:
        parser.error("--reviewer is required unless --check")
    record(args.reviewer, args.findings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
