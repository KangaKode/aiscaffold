"""
Public-corpus case helpers (local to the public-corpus harness).

Resolve schemes stay here -- do not extend golden-set resolve_payload.
Keep under 150 lines.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASES_PATH = _FIXTURES / "public_corpus_cases.json"
MANIFEST_PATH = _FIXTURES / "public_corpus_manifest.json"
BASELINE_PATH = _FIXTURES / "public_corpus_baseline.json"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_LICENSE_ALLOW = frozenset({"MIT", "CC-BY-4.0"})
EXPECTED = frozenset({"flag", "pass"})
SOURCES = frozenset({"injecagent", "agentdojo", "open_continuity"})
_JINJA_OPEN = "{" + "{"
_JINJA_CLOSE = "}" + "}"


def sha256_payload(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest(manifest: dict) -> list[str]:
    errors: list[str] = []
    sources = manifest.get("sources") or {}
    expected_counts = {"injecagent": 60, "agentdojo": 60, "open_continuity": 30}
    for name, want in expected_counts.items():
        src = sources.get(name)
        if not isinstance(src, dict):
            errors.append(f"manifest missing source {name}")
            continue
        if src.get("license") not in _LICENSE_ALLOW:
            errors.append(f"{name}: license not in allow-list {_LICENSE_ALLOW}")
        if not _SHA_RE.match(str(src.get("commit_sha", ""))):
            errors.append(f"{name}: commit_sha must be 40 hex chars")
        if src.get("case_count") != want:
            errors.append(f"{name}: case_count want {want} got {src.get('case_count')}")
        if not src.get("license_url"):
            errors.append(f"{name}: missing license_url")
    return errors


def validate_cases(doc: dict) -> list[str]:
    errors: list[str] = []
    cases = doc.get("cases") or []
    if len(cases) != 150:
        errors.append(f"expected 150 cases, got {len(cases)}")
    seen: set[str] = set()
    by_source: dict[str, int] = {s: 0 for s in SOURCES}
    for case in cases:
        cid = case.get("id", "<missing>")
        if cid in seen:
            errors.append(f"duplicate id: {cid}")
        seen.add(cid)
        src = case.get("source")
        if src not in SOURCES:
            errors.append(f"{cid}: bad source {src!r}")
        else:
            by_source[src] += 1
        if case.get("expected") not in EXPECTED:
            errors.append(f"{cid}: expected must be flag|pass")
        payload = case.get("payload")
        if not isinstance(payload, str):
            errors.append(f"{cid}: payload must be str")
            continue
        digest = case.get("sha256")
        if digest != sha256_payload(payload):
            errors.append(f"{cid}: sha256 mismatch")
        if _JINJA_OPEN in payload or _JINJA_CLOSE in payload:
            errors.append(f"{cid}: payload contains jinja braces")
    for name, want in (("injecagent", 60), ("agentdojo", 60), ("open_continuity", 30)):
        if by_source.get(name) != want:
            errors.append(f"source {name}: count {by_source.get(name)} != {want}")
    return errors
