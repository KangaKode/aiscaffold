"""Failing-contract tests for the pip-audit gate script.

These tests pin the contract Task 4 must satisfy for
``template/{{project_slug}}/scripts/pip_audit_gate.py``: a fail-closed
wrapper around ``pip-audit`` that reads a machine-readable exceptions
allowlist (JSON list of ``{id, reason, owner, compensating_control,
expires}`` objects), validates it BEFORE invoking pip-audit, converts
active entries into explicit ``--ignore-vuln`` arguments, and returns
pip-audit's exact exit code.

What these tests prove:

- Malformed, duplicate-ID, and expired entries fail closed BEFORE
  pip-audit is invoked (an expired exception cannot silently pass through
  to the auditor and be re-honoured on stale data).
- Valid active entries become explicit ``--ignore-vuln <id>`` arguments.
- The gate returns pip-audit's exact exit code (nonzero from pip-audit
  propagates; the gate never swallows a scanner failure).

What they do NOT prove:

- That ``pip-audit`` itself correctly identifies advisories at runtime;
  that is pip-audit's responsibility, exercised in CI.
- That the exceptions file has been reviewed by a human for reachability;
  that is a manual gate documented in GOVERNANCE.

Task 3 is TDD: these tests must fail because the gate script does not yet
exist. Each test imports the module by absolute path via ``importlib`` and
fails with a clear message when the file is absent, rather than using
``try/except ImportError: pass`` that would hide the missing contract.
"""

from datetime import date, timedelta
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = (
    REPO_ROOT
    / "template"
    / "{{project_slug}}"
    / "scripts"
    / "pip_audit_gate.py"
)


def _load_gate() -> types.ModuleType:
    """Import ``pip_audit_gate.py`` by absolute path.

    Raises ``FileNotFoundError`` with a clear message if the script does
    not exist yet; Task 4 creates it. Tests convert this to ``self.fail``
    so discovery does not error out with a confusing ``ModuleNotFoundError``.
    """
    if not GATE_PATH.exists():
        raise FileNotFoundError(
            f"pip_audit_gate.py not found at {GATE_PATH}. Task 4 must "
            "create the fail-closed pip-audit wrapper at that path."
        )
    spec = importlib.util.spec_from_file_location(
        "pip_audit_gate_under_test", GATE_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot build import spec for {GATE_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Cache under a distinct name so tests can freely monkey-patch it.
    sys.modules["pip_audit_gate_under_test"] = module
    spec.loader.exec_module(module)
    return module


class GateModuleAvailabilityTests(unittest.TestCase):
    """Sanity check: the gate module must exist and be importable."""

    def test_gate_script_exists_at_canonical_path(self):
        self.assertTrue(
            GATE_PATH.exists(),
            f"pip_audit_gate.py must ship at the canonical path {GATE_PATH}. "
            "The root workflow invokes the same file; there is no second "
            "copy at the repository root.",
        )

    def test_gate_module_imports(self):
        try:
            _load_gate()
        except FileNotFoundError as exc:
            self.fail(str(exc))


class ExceptionsFileValidationTests(unittest.TestCase):
    """``load_exceptions`` must reject malformed, duplicate, and expired entries."""

    def setUp(self):
        try:
            self.gate = _load_gate()
        except FileNotFoundError as exc:
            self.fail(str(exc))
        self.assertTrue(
            hasattr(self.gate, "load_exceptions"),
            "pip_audit_gate must expose 'load_exceptions(path, today=None)'.",
        )
        self.assertTrue(
            hasattr(self.gate, "ExceptionsError"),
            "pip_audit_gate must expose an 'ExceptionsError' exception class "
            "so callers can distinguish gate errors from pip-audit errors.",
        )

    def _tempdir(self) -> Path:
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="pip_audit_gate_test_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return Path(tmp)

    def _write_exceptions(self, entries) -> Path:
        path = self._tempdir() / "exceptions.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        return path

    def _valid_entry(self, **overrides) -> dict:
        base = {
            "id": "GHSA-aaaa-bbbb-cccc",
            "reason": "Not reachable: only used in unrelated codepath.",
            "owner": "security-team",
            "compensating_control": "Bandit rule B123 covers the sink.",
            "expires": (date.today() + timedelta(days=30)).isoformat(),
        }
        base.update(overrides)
        return base

    def test_valid_active_entry_survives_validation(self):
        path = self._write_exceptions([self._valid_entry()])
        active = self.gate.load_exceptions(path)
        self.assertEqual(
            len(active),
            1,
            "A single valid, unexpired entry must be returned by load_exceptions.",
        )
        self.assertEqual(active[0]["id"], "GHSA-aaaa-bbbb-cccc")

    def test_empty_list_is_valid(self):
        path = self._write_exceptions([])
        active = self.gate.load_exceptions(path)
        self.assertEqual(
            active,
            [],
            "An empty exceptions list must load cleanly; both allowlists "
            "start empty.",
        )

    def test_missing_required_field_raises(self):
        # Any missing key from {id, reason, owner, compensating_control,
        # expires} must fail closed.
        for field in ("id", "reason", "owner", "compensating_control", "expires"):
            with self.subTest(missing=field):
                entry = self._valid_entry()
                entry.pop(field)
                path = self._write_exceptions([entry])
                with self.assertRaises(
                    self.gate.ExceptionsError,
                    msg=(
                        f"Exception entry missing required field {field!r} "
                        "must raise ExceptionsError before pip-audit runs."
                    ),
                ):
                    self.gate.load_exceptions(path)

    def test_duplicate_ids_raise(self):
        path = self._write_exceptions(
            [self._valid_entry(), self._valid_entry()]
        )
        with self.assertRaises(
            self.gate.ExceptionsError,
            msg="Two exception entries with the same advisory ID must "
            "raise ExceptionsError (silent merge would hide reachability "
            "disagreement between two owners).",
        ):
            self.gate.load_exceptions(path)

    def test_malformed_date_raises(self):
        for bad_date in ("2026-13-99", "not-a-date", "2026/07/26", "20260726"):
            with self.subTest(expires=bad_date):
                path = self._write_exceptions(
                    [self._valid_entry(expires=bad_date)]
                )
                with self.assertRaises(
                    self.gate.ExceptionsError,
                    msg=(
                        f"Exception with malformed expires={bad_date!r} must "
                        "raise ExceptionsError; only ISO-8601 dates are valid."
                    ),
                ):
                    self.gate.load_exceptions(path)

    def test_expired_entry_raises(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        path = self._write_exceptions([self._valid_entry(expires=yesterday)])
        with self.assertRaises(
            self.gate.ExceptionsError,
            msg="Expired exception entries must raise ExceptionsError so "
            "blocking behavior is restored automatically at the deadline.",
        ):
            self.gate.load_exceptions(path)

    def test_non_list_top_level_raises(self):
        path = self._tempdir() / "exceptions.json"
        path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        with self.assertRaises(
            self.gate.ExceptionsError,
            msg="Exceptions file whose top-level JSON is not a list must "
            "raise ExceptionsError (schema is a list of objects).",
        ):
            self.gate.load_exceptions(path)


class IgnoreArgumentsTests(unittest.TestCase):
    """Active exceptions must become explicit ``--ignore-vuln`` arguments."""

    def setUp(self):
        try:
            self.gate = _load_gate()
        except FileNotFoundError as exc:
            self.fail(str(exc))
        self.assertTrue(
            hasattr(self.gate, "build_ignore_args"),
            "pip_audit_gate must expose 'build_ignore_args(active_entries)' "
            "returning ['--ignore-vuln', <id>, ...] pairs.",
        )

    def test_active_entries_become_explicit_ignore_vuln_pairs(self):
        entries = [
            {
                "id": "GHSA-aaaa-bbbb-cccc",
                "reason": "unreachable",
                "owner": "sec",
                "compensating_control": "bandit rule",
                "expires": (date.today() + timedelta(days=1)).isoformat(),
            },
            {
                "id": "PYSEC-2026-0001",
                "reason": "unreachable",
                "owner": "sec",
                "compensating_control": "bandit rule",
                "expires": (date.today() + timedelta(days=1)).isoformat(),
            },
        ]
        args = self.gate.build_ignore_args(entries)
        self.assertEqual(
            args,
            [
                "--ignore-vuln",
                "GHSA-aaaa-bbbb-cccc",
                "--ignore-vuln",
                "PYSEC-2026-0001",
            ],
            "build_ignore_args must emit one explicit '--ignore-vuln <id>' "
            "pair per active exception, in order, and no blanket bypass "
            "flags.",
        )

    def test_empty_input_produces_no_arguments(self):
        self.assertEqual(
            self.gate.build_ignore_args([]),
            [],
            "No active exceptions must produce no arguments; the gate "
            "never invents a blanket bypass.",
        )


class PipAuditExitCodePropagationTests(unittest.TestCase):
    """``main`` must return pip-audit's exact exit code (never swallow it)."""

    def setUp(self):
        try:
            self.gate = _load_gate()
        except FileNotFoundError as exc:
            self.fail(str(exc))
        for name in ("main", "run_pip_audit"):
            self.assertTrue(
                hasattr(self.gate, name),
                f"pip_audit_gate must expose module-level {name!r} so the "
                "test can monkey-patch the pip-audit invocation and assert "
                "exit-code propagation.",
            )

    def _write_exceptions(self, entries) -> Path:
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="pip_audit_gate_exit_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = Path(tmp) / "exceptions.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        return path

    def test_pip_audit_zero_exit_propagates(self):
        path = self._write_exceptions([])
        self.gate.run_pip_audit = lambda args: 0
        rc = self.gate.main(["--exceptions", str(path)])
        self.assertEqual(
            rc,
            0,
            "Gate must return 0 verbatim when pip-audit exits 0.",
        )

    def test_pip_audit_nonzero_exit_propagates(self):
        path = self._write_exceptions([])
        for expected in (1, 2, 42):
            with self.subTest(pip_audit_rc=expected):
                self.gate.run_pip_audit = lambda args, expected=expected: expected
                rc = self.gate.main(["--exceptions", str(path)])
                self.assertEqual(
                    rc,
                    expected,
                    "Gate must return pip-audit's exact exit code, never "
                    "collapse it to a canned status.",
                )

    def test_expired_entry_never_reaches_pip_audit(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        path = self._write_exceptions(
            [
                {
                    "id": "GHSA-expired-000",
                    "reason": "was fine, now expired",
                    "owner": "sec",
                    "compensating_control": "n/a",
                    "expires": yesterday,
                }
            ]
        )
        calls: list[list[str]] = []

        def sentinel(args):
            calls.append(list(args))
            return 0

        self.gate.run_pip_audit = sentinel
        rc = self.gate.main(["--exceptions", str(path)])
        self.assertNotEqual(
            rc,
            0,
            "Gate must exit nonzero when the exceptions file contains an "
            "expired entry; a stale exception cannot be silently honoured.",
        )
        self.assertEqual(
            calls,
            [],
            "Gate must fail closed BEFORE invoking pip-audit when the "
            "exceptions file is invalid; pip-audit received "
            f"{calls!r}.",
        )

    def test_active_entry_reaches_pip_audit_as_ignore_vuln(self):
        path = self._write_exceptions(
            [
                {
                    "id": "GHSA-aaaa-bbbb-cccc",
                    "reason": "unreachable in this app",
                    "owner": "sec",
                    "compensating_control": "bandit rule B123",
                    "expires": (date.today() + timedelta(days=7)).isoformat(),
                }
            ]
        )
        captured: list[list[str]] = []

        def sentinel(args):
            captured.append(list(args))
            return 0

        self.gate.run_pip_audit = sentinel
        rc = self.gate.main(["--exceptions", str(path)])
        self.assertEqual(rc, 0, "Gate must propagate pip-audit's zero exit.")
        self.assertEqual(
            len(captured),
            1,
            "Gate must invoke pip-audit exactly once for a valid exceptions file.",
        )
        args = captured[0]
        self.assertIn(
            "--ignore-vuln",
            args,
            "Active exception must be passed to pip-audit as --ignore-vuln.",
        )
        self.assertIn(
            "GHSA-aaaa-bbbb-cccc",
            args,
            "Active exception's advisory ID must appear in the pip-audit args.",
        )


if __name__ == "__main__":
    unittest.main()
