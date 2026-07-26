#!/usr/bin/env python3
"""Fail-closed wrapper around ``pip-audit``.

Keep this file under 300 lines.

Reads a JSON exceptions allowlist that documents each vulnerability the
project has deliberately chosen to ignore for now, validates the file
BEFORE invoking ``pip-audit``, converts each active exception into an
explicit ``--ignore-vuln <id>`` argument pair, then runs ``pip-audit``
and returns its exact exit code.

Contract (pinned by ``tests/test_pip_audit_gate.py`` in the scaffold
repository):

- Exceptions file schema: a JSON list of objects, each with keys
  ``id``, ``reason``, ``owner``, ``compensating_control``, and
  ``expires`` (ISO 8601 ``YYYY-MM-DD``). Empty list is valid.
- Any missing key, duplicate ``id``, malformed date, expired entry, or
  non-list top-level document raises ``ExceptionsError`` BEFORE
  ``pip-audit`` is invoked.
- Every surviving (active) entry becomes an explicit
  ``--ignore-vuln <id>`` pair, in file order. No blanket bypass.
- ``main`` returns ``pip-audit``'s exact exit code (never swallows a
  scanner failure).

This file is shipped both at the root of the scaffold repository (via
its inclusion under the template's project-slug subdirectory, which
the root CI invokes directly) and inside every generated project
(copied through by Copier -- no Jinja placeholders in this file).
There is only one canonical copy; a second at the repo root would
silently drift.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence


REQUIRED_FIELDS = ("id", "reason", "owner", "compensating_control", "expires")

# Strict extended ISO 8601 calendar date: exactly ``YYYY-MM-DD``. The
# builtin ``datetime.date.fromisoformat`` accepts the compact
# ``YYYYMMDD`` form on Python >= 3.11, which the schema forbids
# (typo-prone; a missing dash silently accepts ``20260726``). This
# regex is a strict pre-check; the parser below still validates the
# calendar itself (rejects 2026-13-99 etc.).
_STRICT_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ExceptionsError(Exception):
    """Raised when the exceptions file cannot be parsed or is invalid.

    Callers (the ``main`` entrypoint and, potentially, other tooling)
    catch this to distinguish a gate-configuration failure from a
    ``pip-audit`` failure. The gate returns a distinct nonzero exit
    code in that case so operators can tell "the allowlist is broken"
    apart from "pip-audit found something".
    """


def _parse_iso_date(value: str) -> date:
    """Parse a strict ISO 8601 ``YYYY-MM-DD`` date.

    ``datetime.date.fromisoformat`` accepts only the canonical form
    (rejecting ``2026/07/26``, ``20260726``, ``2026-13-99`` etc.),
    which matches the schema promised in generated GOVERNANCE.
    """

    if not _STRICT_ISO_DATE_RE.match(value):
        raise ValueError(f"not in strict YYYY-MM-DD form: {value!r}")
    return date.fromisoformat(value)


def load_exceptions(
    path: Path | str,
    today: date | None = None,
) -> list[dict]:
    """Read, validate, and return active (unexpired) exceptions.

    Every failure mode is distinguishable:

    - Missing required field -> ``ExceptionsError`` with the field name
      and the word "missing".
    - Duplicate ``id`` -> ``ExceptionsError`` containing "duplicate".
    - Malformed ``expires`` -> ``ExceptionsError`` containing one of
      "invalid" / "malformed" / "parse" / "iso" / "format" plus the
      offending date literal, so operators can jump straight to the
      row.
    - ``expires < today`` -> ``ExceptionsError`` containing "expired"
      (blocking behavior is restored automatically at the deadline).
    - Non-list top-level document -> ``ExceptionsError`` (schema
      violation).

    ``today`` is exposed so operators can dry-run a future date; the
    CI path uses the process default.
    """

    reference_day = today if today is not None else date.today()
    path = Path(path)
    raw_text = path.read_text(encoding="utf-8")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ExceptionsError(
            f"Exceptions file {path} is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})."
        ) from exc

    if not isinstance(data, list):
        raise ExceptionsError(
            f"Exceptions file {path} must contain a JSON list at the "
            f"top level (got {type(data).__name__}). Schema: a list of "
            "objects with keys "
            + ", ".join(repr(k) for k in REQUIRED_FIELDS)
            + "."
        )

    active: list[dict] = []
    seen_ids: set[str] = set()

    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ExceptionsError(
                f"Exceptions file {path} entry #{index} is not a JSON "
                f"object (got {type(entry).__name__}); each exception "
                "must be an object with keys "
                + ", ".join(repr(k) for k in REQUIRED_FIELDS)
                + "."
            )

        for field in REQUIRED_FIELDS:
            if field not in entry:
                raise ExceptionsError(
                    f"Exceptions file {path} entry #{index} is missing "
                    f"required field {field!r}. Every exception must "
                    "declare id, reason, owner, compensating_control, "
                    "and expires."
                )

        entry_id = entry["id"]
        expires_raw = entry["expires"]

        try:
            expires_date = _parse_iso_date(str(expires_raw))
        except (TypeError, ValueError):
            raise ExceptionsError(
                f"Exceptions file {path} entry #{index} (id={entry_id!r}) "
                f"has an invalid, non-ISO-8601 expires value "
                f"{expires_raw!r}. Expected YYYY-MM-DD (e.g. "
                "2026-12-31); malformed dates cannot be silently "
                "honoured because a typo would extend the exception "
                "indefinitely."
            ) from None

        if entry_id in seen_ids:
            raise ExceptionsError(
                f"Exceptions file {path} contains duplicate id "
                f"{entry_id!r}. Two entries with the same id would let "
                "two owners register conflicting reachability claims "
                "for the same advisory; merge them into one row with a "
                "single owner and compensating control."
            )
        seen_ids.add(entry_id)

        if expires_date < reference_day:
            raise ExceptionsError(
                f"Exceptions file {path} entry #{index} (id={entry_id!r}) "
                f"is expired: expires={expires_date.isoformat()} < "
                f"today={reference_day.isoformat()}. Renew the review "
                "(new owner, refreshed reason, extended expires) or "
                "let blocking behaviour resume; a stale exception "
                "cannot be silently honoured."
            )

        active.append(entry)

    return active


def build_ignore_args(active_entries: Iterable[dict]) -> list[str]:
    """Convert active exceptions into ``--ignore-vuln <id>`` pairs.

    Each pair is emitted adjacently and in input order. ``pip-audit``
    reads ``--ignore-vuln`` as a two-token flag-value pair, so
    adjacency matters: splitting the flags and values apart would let
    ``pip-audit`` reparse the ids as positional arguments and silently
    drop the intended ignore semantics.
    """

    args: list[str] = []
    for entry in active_entries:
        args.append("--ignore-vuln")
        args.append(str(entry["id"]))
    return args


def run_pip_audit(args: Sequence[str]) -> int:
    """Invoke ``pip-audit`` with ``args`` and return its exit code.

    Uses the current interpreter's ``python -m pip_audit`` entry so
    the auditor version follows the pinned install in CI. Tests
    monkey-patch this function to assert argument shape without
    hitting the network.
    """

    completed = subprocess.run(
        [sys.executable, "-m", "pip_audit", *args],
        check=False,
    )
    return completed.returncode


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pip_audit_gate",
        description=(
            "Run pip-audit through a fail-closed exceptions gate. Any "
            "additional arguments are forwarded verbatim to pip-audit "
            "after the --ignore-vuln pairs derived from the exceptions "
            "file."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--exceptions",
        required=True,
        help=(
            "Path to the JSON exceptions allowlist. Must be a list of "
            "objects with keys id, reason, owner, compensating_control, "
            "expires (ISO 8601 YYYY-MM-DD). Empty list is valid."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entrypoint.

    Returns pip-audit's exact exit code on a valid exceptions file.
    Returns a distinct nonzero code (``2``) when the exceptions file
    itself is invalid, so operators can tell "gate is broken" apart
    from "pip-audit found a real vulnerability".
    """

    parser = _build_arg_parser()
    args, extras = parser.parse_known_args(list(argv) if argv is not None else None)

    try:
        active = load_exceptions(Path(args.exceptions))
    except ExceptionsError as exc:
        print(f"[pip_audit_gate] {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            f"[pip_audit_gate] Cannot read exceptions file "
            f"{args.exceptions!r}: {exc}",
            file=sys.stderr,
        )
        return 2

    ignore_args = build_ignore_args(active)
    pip_audit_args = ignore_args + list(extras)

    if active:
        summary = ", ".join(entry["id"] for entry in active)
        print(
            f"[pip_audit_gate] Ignoring {len(active)} exception(s): {summary}",
            file=sys.stderr,
        )

    return run_pip_audit(pip_audit_args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
