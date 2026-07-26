#!/usr/bin/env python3
"""Fail-closed wrapper around ``pip-audit``.

Keep this file under 400 lines.

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
- ``main`` returns ``pip-audit``'s exact exit code when pip-audit
  actually ran (never swallows a scanner failure).

Exit codes:

- ``0`` / ``1`` / ``2`` / any code returned by ``pip-audit`` --
  propagated verbatim when pip-audit was launched successfully.
  ``pip-audit`` uses ``0`` for a clean audit, ``1`` for advisories
  found, and ``2`` for its own argument errors.
- ``GATE_CONFIG_ERROR`` (``78``, chosen from BSD ``sysexits.h``
  ``EX_CONFIG``) for gate-configuration failures: an invalid or
  unreadable exceptions file, an unexpected error while parsing the
  allowlist, or an environmental error that prevents ``pip-audit``
  from being launched at all (for example, the interpreter path is
  unusable). Distinct from ``pip-audit``'s own ``2`` so an operator
  can tell "the allowlist is broken" apart from "pip-audit rejected
  its arguments". Every gate-config failure path prints a
  ``[pip_audit_gate]``-prefixed diagnostic on stderr; the gate never
  returns ``0`` on any failure path.

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

# Distinct exit code for gate-configuration failures. Chosen from BSD
# ``sysexits.h`` ``EX_CONFIG`` (78). Kept separate from ``pip-audit``'s
# own 0/1/2 so an operator can tell "the allowlist is broken" (this
# code) apart from "pip-audit rejected its arguments" (pip-audit's own
# ``2``). Any change to this value must be reflected in the module
# docstring above.
GATE_CONFIG_ERROR = 78

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
    ``pip-audit`` failure. The gate returns ``GATE_CONFIG_ERROR`` in
    that case (see module docstring) so operators can tell "the
    allowlist is broken" apart from "pip-audit found something" or
    "pip-audit rejected its arguments".
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

    Fail-closed guard: an ``OSError`` from ``subprocess.run`` (e.g.
    ``FileNotFoundError`` when ``sys.executable`` is unusable, or
    ``PermissionError``) is translated into a
    ``[pip_audit_gate]``-prefixed stderr message and a
    ``GATE_CONFIG_ERROR`` return. Letting the raw traceback out would
    still exit nonzero via Python's default handler, but a clear
    diagnostic is what an operator needs to fix the environment.
    """

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip_audit", *args],
            check=False,
        )
    except OSError as exc:
        print(
            f"[pip_audit_gate] Failed to invoke pip-audit via "
            f"{sys.executable!r} -m pip_audit "
            f"({exc.__class__.__name__}: {exc}). Ensure the interpreter "
            "is executable and the ``pip_audit`` package is installed "
            "in that interpreter's environment.",
            file=sys.stderr,
        )
        return GATE_CONFIG_ERROR
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

    Returns ``pip-audit``'s exact exit code on a valid exceptions
    file. Returns ``GATE_CONFIG_ERROR`` (see module docstring) when
    the exceptions file itself is invalid or unreadable, when the
    pip-audit subprocess cannot be launched, or when any other
    unexpected exception is raised while preparing or running the
    gate -- so operators can tell "gate is broken" apart from
    "pip-audit found a real vulnerability" or "pip-audit rejected
    its arguments" (its own ``2``). Every failure path prints a
    ``[pip_audit_gate]``-prefixed stderr diagnostic; no failure path
    returns ``0``.
    """

    parser = _build_arg_parser()
    args, extras = parser.parse_known_args(list(argv) if argv is not None else None)

    try:
        active = load_exceptions(Path(args.exceptions))
    except ExceptionsError as exc:
        print(f"[pip_audit_gate] {exc}", file=sys.stderr)
        return GATE_CONFIG_ERROR
    except OSError as exc:
        print(
            f"[pip_audit_gate] Cannot read exceptions file "
            f"{args.exceptions!r}: {exc}",
            file=sys.stderr,
        )
        return GATE_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001 -- normalize; gate must fail closed with a diagnostic
        # Defense in depth: any unexpected error while loading the
        # allowlist (e.g. a corrupt filesystem raising something
        # exotic, or a future validator raising something other than
        # ExceptionsError) becomes a clear diagnostic + nonzero exit
        # rather than a raw traceback.
        print(
            f"[pip_audit_gate] Unexpected error while loading exceptions "
            f"file {args.exceptions!r}: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return GATE_CONFIG_ERROR

    ignore_args = build_ignore_args(active)
    pip_audit_args = ignore_args + list(extras)

    if active:
        summary = ", ".join(entry["id"] for entry in active)
        print(
            f"[pip_audit_gate] Ignoring {len(active)} exception(s): {summary}",
            file=sys.stderr,
        )

    try:
        return run_pip_audit(pip_audit_args)
    except Exception as exc:  # noqa: BLE001 -- normalize; gate must fail closed with a diagnostic
        # ``run_pip_audit`` already catches ``OSError`` from
        # ``subprocess.run`` itself and returns ``GATE_CONFIG_ERROR``.
        # This outer guard covers any other unexpected exception
        # (custom monkey-patched sentinel, future refactor, etc.) so
        # the gate never returns ``0`` on any failure path.
        print(
            f"[pip_audit_gate] Unexpected error while running pip-audit: "
            f"{exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return GATE_CONFIG_ERROR


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
