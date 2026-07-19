#!/usr/bin/env python3
"""
Maintainer-only refresh helper for public-corpus eval fixtures.

Repo-root only -- never ships into generated projects. Refuses unless
PUBLIC_CORPUS_REFRESH=1 and not running under CI/GITHUB_ACTIONS.

Default is --dry-run (print plan). --confirm writes after verify.

Usage (from roundtable repo root, never in CI):
  PUBLIC_CORPUS_REFRESH=1 python scripts/refresh_public_corpus.py --dry-run
  PUBLIC_CORPUS_REFRESH=1 python scripts/refresh_public_corpus.py --confirm

Keep under 250 lines.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = (
    REPO_ROOT / "template" / "{{project_slug}}" / "evals" / "fixtures"
)
MANIFEST_PATH = FIXTURE_DIR / "public_corpus_manifest.json"
CASES_PATH = FIXTURE_DIR / "public_corpus_cases.json"
BASELINE_PATH = FIXTURE_DIR / "public_corpus_baseline.json"

_MAX_BYTES = 8_000_000
_MAX_CASES = 150
_RAW = "https://raw.githubusercontent.com/{repo}/{sha}/{path}"


def _die(msg: str, code: int = 2) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _gate() -> None:
    if os.environ.get("PUBLIC_CORPUS_REFRESH") != "1":
        _die("refusing: set PUBLIC_CORPUS_REFRESH=1 (maintainer-only)")
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        _die("refusing: refresh is disabled under CI/GITHUB_ACTIONS")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch(repo: str, sha: str, path: str) -> bytes:
    url = _RAW.format(repo=repo, sha=sha, path=path)
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310
            data = resp.read(_MAX_BYTES + 1)
    except urllib.error.URLError as exc:
        _die(f"fetch failed for {url}: {exc}")
    if len(data) > _MAX_BYTES:
        _die(f"fetch exceeded {_MAX_BYTES} bytes: {url}")
    return data


def _verify_local() -> dict:
    if not MANIFEST_PATH.is_file() or not CASES_PATH.is_file():
        _die("fixtures missing; generate them before refresh")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    cases_doc = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = cases_doc.get("cases") or []
    if len(cases) > _MAX_CASES:
        _die(f"case count {len(cases)} exceeds cap {_MAX_CASES}")
    bad = []
    for case in cases:
        payload = case.get("payload", "")
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if digest != case.get("sha256"):
            bad.append(case.get("id", "?"))
    if bad:
        _die(f"sha256 mismatch on cases: {bad[:5]}")
    for name, src in (manifest.get("sources") or {}).items():
        lic = src.get("license")
        if lic not in ("MIT", "CC-BY-4.0"):
            _die(f"{name}: license {lic!r} not allow-listed")
        sha = str(src.get("commit_sha", ""))
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            _die(f"{name}: bad commit_sha")
    return {"manifest": manifest, "cases": len(cases)}


def _probe_upstream(manifest: dict) -> list[str]:
    """Fetch pinned upstream blobs and report digest (does not rewrite cases)."""
    notes: list[str] = []
    inj = manifest["sources"]["injecagent"]
    ad = manifest["sources"]["agentdojo"]
    probes = [
        ("uiuc-kang-lab/InjecAgent", inj["commit_sha"], "data/test_cases_dh_base.json"),
        ("uiuc-kang-lab/InjecAgent", inj["commit_sha"], "LICENSE"),
        (
            "ethz-spylab/agentdojo",
            ad["commit_sha"],
            "src/agentdojo/attacks/baseline_attacks.py",
        ),
        ("ethz-spylab/agentdojo", ad["commit_sha"], "LICENSE"),
    ]
    for repo, sha, path in probes:
        data = _fetch(repo, sha, path)
        notes.append(f"ok {repo}@{sha[:12]} {path} sha256={_sha256_bytes(data)[:16]}…")
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="write is a no-op for selection rebuild in v1; still verifies + probes",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="default: verify local fixtures and print plan (no network)",
    )
    parser.add_argument(
        "--probe-network",
        action="store_true",
        help="with --confirm: fetch pinned upstream blobs and print digests",
    )
    args = parser.parse_args(argv)
    _gate()

    summary = _verify_local()
    print("Local fixtures OK:")
    print(f"  cases={summary['cases']} (cap {_MAX_CASES})")
    for name, src in summary["manifest"]["sources"].items():
        print(
            f"  {name}: license={src['license']} "
            f"sha={src['commit_sha'][:12]}… count={src['case_count']}"
        )

    if args.confirm and args.probe_network:
        print("\nProbing pinned upstream blobs…")
        for line in _probe_upstream(summary["manifest"]):
            print(f"  {line}")
        print(
            "\nNote: case reselection is intentional/manual. "
            "After changing selection, re-grade with the harness "
            "--update-baseline and commit fixtures + ATTRIBUTION together."
        )
        return 0

    if args.confirm and not args.probe_network:
        print(
            "\n--confirm without --probe-network: local verify only "
            "(no writes; selection rebuild is manual)."
        )
        return 0

    print(
        "\nDry-run complete. Re-run with "
        "--confirm --probe-network to fetch pinned upstream digests."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
