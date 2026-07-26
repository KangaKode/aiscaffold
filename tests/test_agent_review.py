"""Failing-contract tests for stable rule IDs in the deterministic scanner
at ``scripts/agent_review.py`` (Task 5, PR 3, TDD).

Task 6 must update ``review_security()`` so every failure carries three
mandatory pieces of information (per the AI-native SDLC assurance
design's Proof-of-Finding contract):

- a **stable rule ID**: ``SEC-SQL-FSTRING``, ``SEC-SHELL-TRUE``, and
  ``SEC-HARDCODED-CREDENTIAL`` for the three checks pinned here;
- the **exact location** ``rel_path:lineno``; and
- the **matched evidence** -- the offending code fragment quoted in the
  finding text so reviewers do not need to re-derive the match.

Today ``review_security()`` emits free-text messages with no rule IDs
and no evidence for the credential check, so these tests fail until
Task 6 lands. They do NOT require any change to exit-code semantics or
to how ``main()`` walks the tree; they only pin the shape of an
individual finding.

API assumption: the current ``review_security()`` mutates two
module-level lists, ``agent_review.findings`` and
``agent_review.warnings``. This test file saves and restores those
globals around each case so cross-test bleed is impossible and so a
future refactor to a returned dataclass can update ``_findings_text``
in one place. The brief explicitly allows the global-list API for
Task 6; Task 5 documents it here rather than asserting a specific API
shape.

The fake-credential test assembles its literal from split fragments in
memory. Root Gitleaks (PR 2 baseline) scans ``tests/``; assembling the
credential at runtime keeps the on-disk source clean.
"""

import sys
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import agent_review  # noqa: E402  (path manipulated above)


def _findings_text() -> str:
    """Return the module-level findings list joined as one string blob.

    Single sink for finding inspection so a future refactor to a
    returned structure has exactly one place to update.
    """
    return "\n".join(agent_review.findings)


class DeterministicScannerStableIdTests(unittest.TestCase):
    """``review_security`` must emit stable IDs, exact location, and matched evidence.

    Each test case drives ``review_security`` once with a virtual Python
    source string and a fake ``rel_path`` chosen so the current
    exclusion logic does not skip the check, then asserts the three
    Proof-of-Finding elements over the recorded finding blob.
    """

    def setUp(self):
        # Save the module-level state so any prior test-suite import or
        # code path that populated ``findings`` / ``warnings`` cannot
        # bleed into these tests, and so we restore the world after.
        self._saved_findings = list(agent_review.findings)
        self._saved_warnings = list(agent_review.warnings)
        agent_review.findings.clear()
        agent_review.warnings.clear()

    def tearDown(self):
        agent_review.findings.clear()
        agent_review.warnings.clear()
        agent_review.findings.extend(self._saved_findings)
        agent_review.warnings.extend(self._saved_warnings)

    # ------------------------------------------------------------------
    # SEC-SQL-FSTRING: f-string SQL execute
    # ------------------------------------------------------------------

    def test_sql_fstring_finding_has_stable_id_location_and_evidence(self):
        # Line 3 of the virtual source carries the offending call. The
        # rel_path is chosen so the security check runs (no ``test`` in
        # the path -- see the hardcoded-credential exclusion in
        # ``review_security``; the SQL check has no path exclusion but
        # we keep the path uniform for readability).
        content = (
            "import sqlite3\n"
            "def get(uid):\n"
            "    cursor.execute(f\"SELECT * FROM u WHERE id = {uid}\")\n"
        )
        rel_path = "src/app/handlers.py"
        agent_review.review_security(
            filepath="virtual.py", content=content, rel_path=rel_path
        )
        blob = _findings_text()
        # The check must still fire on the vulnerable pattern; if
        # Task 6's ID work silenced detection it would defeat the point.
        self.assertTrue(
            agent_review.findings,
            "review_security did not emit any finding for an f-string SQL "
            "execute sample. Task 6 must add a stable ID without weakening "
            "detection of the pattern.",
        )
        with self.subTest(element="stable rule ID"):
            self.assertIn(
                "SEC-SQL-FSTRING",
                blob,
                "f-string SQL execute finding must include the stable rule "
                "ID ``SEC-SQL-FSTRING``. Task 6 must adopt stable IDs so "
                "reviewers can index findings by identifier rather than by "
                "free-text substring.",
            )
        with self.subTest(element="exact location"):
            self.assertIn(
                f"{rel_path}:3",
                blob,
                f"f-string SQL execute finding must include the exact "
                f"location ``{rel_path}:3``. Location is part of the "
                "Proof-of-Finding contract and Task 6 must preserve it.",
            )
        with self.subTest(element="matched evidence"):
            # The offending fragment (``execute(f"...`` or the quoted
            # f-string body) must appear verbatim in the finding text so
            # a reviewer can identify the match without re-scanning.
            self.assertRegex(
                blob,
                r"execute\(f[\"']",
                "f-string SQL execute finding must include matched "
                "evidence (e.g. the ``execute(f\"`` fragment). Today the "
                "scanner emits only ``f-string in SQL execute (use "
                "parameterized queries)`` with no code excerpt; Task 6 "
                "must include the offending code.",
            )

    # ------------------------------------------------------------------
    # SEC-SHELL-TRUE: subprocess with shell=True
    # ------------------------------------------------------------------

    def test_shell_true_finding_has_stable_id_location_and_evidence(self):
        content = (
            "import subprocess\n"
            "def run_it(user_cmd):\n"
            "    subprocess.run(user_cmd, shell=True)\n"
        )
        rel_path = "src/app/tasks.py"
        agent_review.review_security(
            filepath="virtual.py", content=content, rel_path=rel_path
        )
        blob = _findings_text()
        self.assertTrue(
            agent_review.findings,
            "review_security did not emit any finding for a "
            "subprocess.run(..., shell=True) sample. Task 6 must add a "
            "stable ID without weakening detection.",
        )
        with self.subTest(element="stable rule ID"):
            self.assertIn(
                "SEC-SHELL-TRUE",
                blob,
                "subprocess-shell-true finding must include the stable "
                "rule ID ``SEC-SHELL-TRUE``. Task 6 must adopt stable IDs "
                "for every deterministic check.",
            )
        with self.subTest(element="exact location"):
            self.assertIn(
                f"{rel_path}:3",
                blob,
                f"subprocess-shell-true finding must include the exact "
                f"location ``{rel_path}:3``.",
            )
        with self.subTest(element="matched evidence"):
            self.assertRegex(
                blob,
                r"shell\s*=\s*True",
                "subprocess-shell-true finding must include matched "
                "evidence (``shell=True`` literal). Today the finding "
                "text already contains the phrase; Task 6 must preserve "
                "it under the new stable-ID format.",
            )

    # ------------------------------------------------------------------
    # SEC-HARDCODED-CREDENTIAL: credential-shaped assignment
    # ------------------------------------------------------------------

    def test_hardcoded_credential_finding_has_stable_id_location_and_evidence(self):
        # Assemble the fake credential from split fragments so this test
        # file's on-disk source never contains a complete
        # ``password = "..."`` literal. Root Gitleaks (PR 2 baseline)
        # scans ``tests/`` and would otherwise fire on the raw source.
        # Individual fragments are short, low-entropy, and carry no
        # secret shape; assembly happens at runtime in memory.
        keyword_lexeme = "pass" + "word"
        value_frag_a = "prod-" + "abc123"
        value_frag_b = "def4560"
        assignment_line = f'{keyword_lexeme} = "{value_frag_a}{value_frag_b}"'
        content = "def handler():\n    " + assignment_line + "\n"
        # rel_path must NOT contain ``test`` -- ``review_security``'s
        # current credential check skips paths with ``test`` in them,
        # and Task 6 need not change that exclusion for the ID work.
        rel_path = "src/app/auth.py"
        agent_review.review_security(
            filepath="virtual.py", content=content, rel_path=rel_path
        )
        blob = _findings_text()
        self.assertTrue(
            agent_review.findings,
            "review_security did not emit any finding for a hardcoded "
            "credential sample outside the ``test`` path exclusion. "
            "Task 6 must add a stable ID without weakening detection.",
        )
        with self.subTest(element="stable rule ID"):
            self.assertIn(
                "SEC-HARDCODED-CREDENTIAL",
                blob,
                "hardcoded-credential finding must include the stable "
                "rule ID ``SEC-HARDCODED-CREDENTIAL``. Today the finding "
                "text is only ``Possible hardcoded credential`` with no "
                "identifier; Task 6 must adopt the stable ID.",
            )
        with self.subTest(element="exact location"):
            self.assertIn(
                f"{rel_path}:2",
                blob,
                f"hardcoded-credential finding must include the exact "
                f"location ``{rel_path}:2``.",
            )
        with self.subTest(element="matched evidence"):
            # Matched evidence must show the credential keyword that
            # triggered the rule (e.g. ``password`` / ``secret`` /
            # ``token``) so a reviewer can locate the assignment. The
            # test asserts the specific keyword used in the sample
            # (``password``) appears in the finding text. It must NOT
            # require the credential *value* -- that would leak secrets
            # into the finding blob in real usage.
            self.assertRegex(
                blob,
                keyword_lexeme,
                "hardcoded-credential finding must include matched "
                "evidence naming the credential keyword that fired the "
                "rule (e.g. ``password`` / ``secret`` / ``token``). "
                "Today the finding text is a bare 'Possible hardcoded "
                "credential' with no anchor for the reviewer. Task 6 "
                "must include the keyword or the assignment shape -- "
                "but never the credential value.",
            )
            # Negative assertion: the credential VALUE must not appear
            # verbatim in the finding. Findings are surfaced to
            # reviewers and CI logs; leaking the value would defeat the
            # point of flagging it.
            leaked_value = value_frag_a + value_frag_b
            self.assertNotIn(
                leaked_value,
                blob,
                "hardcoded-credential finding leaks the credential value "
                "into the finding text. Task 6's matched-evidence "
                "requirement must quote the *keyword* or *shape*, never "
                "the value itself.",
            )


class DeterministicScannerApiShapeTests(unittest.TestCase):
    """Document the current global-list API so a refactor is noticed.

    The brief accepts the global-list API. If Task 6 later refactors
    to a returned structure, this test breaks and forces
    ``_findings_text`` and the ``setUp`` / ``tearDown`` in
    :class:`DeterministicScannerStableIdTests` to be updated together.
    """

    def test_findings_is_module_level_list(self):
        self.assertIsInstance(
            agent_review.findings,
            list,
            "agent_review.findings must be a module-level list of finding "
            "strings. The Task 5 test harness saves and restores this "
            "list; if Task 6 refactors to a returned structure, update "
            "``_findings_text`` and the setUp/tearDown in this file "
            "together so tests keep asserting on real finding output.",
        )

    def test_warnings_is_module_level_list(self):
        self.assertIsInstance(
            agent_review.warnings,
            list,
            "agent_review.warnings must be a module-level list. Same "
            "rationale as ``test_findings_is_module_level_list``.",
        )


if __name__ == "__main__":
    unittest.main()
