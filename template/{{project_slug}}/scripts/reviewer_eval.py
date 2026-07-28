#!/usr/bin/env python3
"""Reviewer-eval deterministic runner (Task 8, PR 4).

Runs only ``DETERMINISTIC`` cases from ``reviewer-evals/cases.json``
against the local ``red_team_check`` scanner functions. Exits nonzero
for any of:

- schema errors in ``cases.json``;
- missed vulnerabilities (``DETERMINISTIC`` vulnerable case whose
  expected rule IDs are NOT emitted by the scanner);
- false-blocks on safe near-misses (``DETERMINISTIC`` safe case where
  the scanner emits ANY rule ID).

MANUAL_AGENT cases are documented in ``reviewer-evals/README.md``.
This runner does NOT execute them: CI does not run prompt reviewers.
A human operator feeds each MANUAL_AGENT case to the scoped prompt
reviewers listed in its ``manual_reviewers`` field via the recipe in
the README.

Guidance verified: 2026-07.

Keep this file under 280 lines. (Bumped from 240 in Task 8 to fit
argument-parser + summary helpers alongside schema validation and
scanner dispatch; splitting for the sake of the cap would fragment
the runner's single-responsibility surface.)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_CASES_PATH = PROJECT_ROOT / "reviewer-evals" / "cases.json"

# ``red_team_check`` ships alongside this module in the generated
# project's ``scripts/`` directory. Insert this directory into
# ``sys.path`` so the import works whether the runner is invoked via
# ``python scripts/reviewer_eval.py`` from the project root or through
# ``importlib.util.spec_from_file_location`` from a test harness.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import red_team_check  # type: ignore  # noqa: E402  -- path adjusted above.

VALID_MODES = ("DETERMINISTIC", "MANUAL_AGENT")
VALID_DISPOSITIONS = ("vulnerable", "safe")

REQUIRED_FIELDS = (
    "id",
    "domain",
    "execution_mode",
    "virtual_path",
    "content_fragments",
    "expected_disposition",
    "expected_rule_ids",
    "manual_reviewers",
)


def load_cases(path: Path | str) -> list[dict[str, Any]]:
    """Load ``cases.json`` from disk and return the parsed list.

    Does NOT validate structure -- ``validate_schema`` is a separate
    step so callers can decide whether a bad fixture aborts the run.
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_schema(cases: Any) -> list[str]:
    """Return a list of human-readable schema errors (empty = valid).

    The runner treats any nonempty return as a fixture regression and
    exits nonzero -- silently skipping malformed cases would let a
    tampered corpus pass CI without covering the domains it claims.
    """
    errors: list[str] = []
    if not isinstance(cases, list):
        return ["cases.json must be a JSON list"]

    seen_ids: list[str] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"case[{index}]: not an object")
            continue

        label = case.get("id", f"<index {index}>")
        for field in REQUIRED_FIELDS:
            if field not in case:
                errors.append(f"case {label!r}: missing field {field!r}")

        if not all(field in case for field in REQUIRED_FIELDS):
            continue

        if case["execution_mode"] not in VALID_MODES:
            errors.append(
                f"case {label!r}: execution_mode {case['execution_mode']!r} "
                f"must be one of {list(VALID_MODES)}"
            )
        if case["expected_disposition"] not in VALID_DISPOSITIONS:
            errors.append(
                f"case {label!r}: expected_disposition "
                f"{case['expected_disposition']!r} must be one of "
                f"{list(VALID_DISPOSITIONS)}"
            )
        if not isinstance(case["content_fragments"], list) or not case["content_fragments"]:
            errors.append(f"case {label!r}: content_fragments must be a nonempty list")
        else:
            for j, fragment in enumerate(case["content_fragments"]):
                if not isinstance(fragment, str):
                    errors.append(
                        f"case {label!r}: content_fragments[{j}] must be a string"
                    )
        if not isinstance(case["expected_rule_ids"], list):
            errors.append(f"case {label!r}: expected_rule_ids must be a list")
        if not isinstance(case["manual_reviewers"], list):
            errors.append(f"case {label!r}: manual_reviewers must be a list")
        if case["execution_mode"] == "MANUAL_AGENT" and case.get("expected_rule_ids"):
            errors.append(
                f"case {label!r}: MANUAL_AGENT case must not declare "
                "deterministic expected_rule_ids -- the runner does not "
                "execute prompt reviewers"
            )
        if case["expected_disposition"] == "safe" and case.get("expected_rule_ids"):
            errors.append(
                f"case {label!r}: safe near-miss must not declare "
                "expected rule IDs (a rule firing on a safe case is a "
                "false-block, not an expected outcome)"
            )
        seen_ids.append(str(case.get("id")))

    duplicate_ids = sorted({cid for cid in seen_ids if seen_ids.count(cid) > 1})
    if duplicate_ids:
        errors.append(f"duplicate case ids: {duplicate_ids}")

    return errors


def _scan(virtual_path: str, content: str) -> set[str]:
    """Run the three deterministic ``red_team_check`` functions in memory.

    Route the case's ``virtual_path`` in as ``red_team_check``'s
    ``fp`` argument so no vulnerable file ever touches disk. The
    scanner functions accept a filepath purely for reporting; they do
    not open it.
    """
    findings: list[Any] = []
    findings.extend(red_team_check.check_secrets(virtual_path, content))
    findings.extend(red_team_check.check_sql_injection(virtual_path, content))
    findings.extend(red_team_check.check_dangerous(virtual_path, content))
    return {finding.rule_id for finding in findings}


def run_deterministic_cases(cases: Iterable[dict[str, Any]]) -> list[str]:
    """Run every ``DETERMINISTIC`` case and return failure messages.

    A ``vulnerable`` case fails when the scanner emits none of its
    expected rule IDs (missed vulnerability). A ``safe`` case fails
    when the scanner emits ANY rule ID (false-block).
    """
    failures: list[str] = []
    for case in cases:
        if case.get("execution_mode") != "DETERMINISTIC":
            continue
        content = "".join(case["content_fragments"])
        emitted = _scan(case["virtual_path"], content)
        expected = set(case.get("expected_rule_ids", []))

        disposition = case.get("expected_disposition")
        case_id = case.get("id", "<unknown>")
        if disposition == "vulnerable":
            missing = expected - emitted
            if missing:
                failures.append(
                    f"MISSED_VULN {case_id!r}: expected rule IDs "
                    f"{sorted(missing)} were not emitted "
                    f"(emitted: {sorted(emitted) or 'none'})"
                )
        elif disposition == "safe":
            if emitted:
                failures.append(
                    f"FALSE_BLOCK {case_id!r}: safe near-miss triggered "
                    f"rule IDs {sorted(emitted)}"
                )
    return failures


def _summarize_manual_cases(cases: Iterable[dict[str, Any]]) -> None:
    """Print a summary of MANUAL_AGENT cases for the operator.

    CI does NOT feed these cases to prompt reviewers -- they are
    listed here so the operator running the deterministic runner sees
    which domains still require human review before shipping.
    """
    manual = [c for c in cases if c.get("execution_mode") == "MANUAL_AGENT"]
    if not manual:
        return
    print(f"MANUAL_AGENT cases ({len(manual)}) -- run these via human review, not CI:")
    for case in manual:
        reviewers = ", ".join(case.get("manual_reviewers", []) or ["<none>"])
        print(f"  - {case['id']} ({case['domain']}, {case['expected_disposition']}); "
              f"reviewers: {reviewers}")
    print("  See reviewer-evals/README.md for the manual-review recipe.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic reviewer-eval cases against red_team_check.",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help="Path to cases.json (default: %(default)s)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the MANUAL_AGENT summary block on success.",
    )
    args = parser.parse_args(argv)

    if not args.cases.exists():
        print(f"reviewer-evals: cases file not found at {args.cases}")
        return 2

    try:
        cases = load_cases(args.cases)
    except json.JSONDecodeError as exc:
        print(f"reviewer-evals: cases.json is not valid JSON: {exc}")
        return 2

    schema_errors = validate_schema(cases)
    if schema_errors:
        print("reviewer-evals: schema errors:")
        for message in schema_errors:
            print(f"  - {message}")
        return 1

    failures = run_deterministic_cases(cases)
    if failures:
        print("reviewer-evals: DETERMINISTIC failures:")
        for message in failures:
            print(f"  - {message}")
        return 1

    print(f"reviewer-evals: PASS ({len(cases)} case(s) validated; "
          f"{sum(1 for c in cases if c['execution_mode'] == 'DETERMINISTIC')} "
          f"DETERMINISTIC case(s) executed).")
    if not args.quiet:
        _summarize_manual_cases(cases)
    return 0


if __name__ == "__main__":
    sys.exit(main())
