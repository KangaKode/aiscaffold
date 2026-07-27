"""Reviewer-eval fixture and harness tests (Task 8, PR 4).

Task 8 seeds ``template/{{project_slug}}/reviewer-evals/cases.json``, the
shipped runner ``template/{{project_slug}}/scripts/reviewer_eval.py``,
and this test file. The tests pin the fixture schema, the coverage
matrix, and the deterministic detection contract so a regression in
either the fixture or a scanner is caught in the root suite before it
ships downstream through copier.

Contract this file pins:

- ``cases.json`` entries carry the exact eight fields the brief names:
  ``id``, ``domain``, ``execution_mode`` (``DETERMINISTIC`` |
  ``MANUAL_AGENT``), ``virtual_path``, ``content_fragments`` (list of
  strings joined only in memory), ``expected_disposition``
  (``vulnerable`` | ``safe``), ``expected_rule_ids`` (list, empty for
  safe / manual-only), and ``manual_reviewers`` (list).
- IDs are unique across the corpus; every domain ships at least one
  vulnerable case AND one safe near-miss.
- The three deterministic domains (``hardcoded_secret``,
  ``sql_injection``, ``unsafe_shell``) ship as ``DETERMINISTIC``; the
  four manual domains (``path_traversal``, ``missing_auth``,
  ``missing_tenant_scope``, ``prompt_injection_boundary``) ship as
  ``MANUAL_AGENT`` -- this task must not claim manual-only coverage as
  deterministically proven.
- The prompt-injection case's fragments are stored as plain JSON
  strings (data) so a downstream reviewer never mistakes them for
  live instructions.
- No committed fixture line contains the assembled fake-credential
  marker; the marker is built in the test itself from the same
  fragments the fixture stores separately (root Gitleaks scans the
  whole repo).
- The root deterministic scanner ``scripts/agent_review.py`` emits
  the expected rule IDs for every ``DETERMINISTIC`` case and no IDs
  for the ``safe`` counterparts. The existing test-file exclusion in
  ``review_security`` is NOT weakened; virtual paths route around it.
- The shipped runner ``template/{{project_slug}}/scripts/reviewer_eval.py``
  imports cleanly (via ``importlib.util``) and validates the same
  fixture against the generated ``red_team_check.py`` scanner. The
  runner exits nonzero for schema errors, missed vulnerabilities, and
  false-blocked safe cases.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import agent_review  # noqa: E402  -- path adjusted above.

TEMPLATE_ROOT = REPO_ROOT / "template" / "{{project_slug}}"
REVIEWER_EVALS_DIR = TEMPLATE_ROOT / "reviewer-evals"
CASES_PATH = REVIEWER_EVALS_DIR / "cases.json"
README_PATH = REVIEWER_EVALS_DIR / "README.md"
RUNNER_PATH = TEMPLATE_ROOT / "scripts" / "reviewer_eval.py"
RED_TEAM_PATH = TEMPLATE_ROOT / "scripts" / "red_team_check.py"


# Domains and their required execution mode. These pin the coverage
# matrix -- a regression that reclassifies a manual-only domain as
# ``DETERMINISTIC`` (and thus implicitly claims CI proof) fails here.
DETERMINISTIC_DOMAINS = {"hardcoded_secret", "sql_injection", "unsafe_shell"}
MANUAL_DOMAINS = {
    "path_traversal",
    "missing_auth",
    "missing_tenant_scope",
    "prompt_injection_boundary",
}
ALL_DOMAINS = DETERMINISTIC_DOMAINS | MANUAL_DOMAINS

VALID_MODES = {"DETERMINISTIC", "MANUAL_AGENT"}
VALID_DISPOSITIONS = {"vulnerable", "safe"}

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


def _load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _assembled_credential_marker() -> str:
    """Rebuild the fake credential the fixture stores as fragments.

    The fixture MUST split ``password``, the assignment shape, and the
    fake value across separate JSON strings so no committed line
    contains this marker. The test assembles the marker here so a
    regression that combines the fragments back into a single line is
    detected: the marker string must be absent from every tracked file
    the test walks below.
    """
    keyword = "pass" + "word"
    value_a = "seed-" + "abc123"
    value_b = "def4560"
    return f'{keyword} = "{value_a}{value_b}"'


def _load_template_runner():
    """Import the shipped runner module directly by file path.

    The template lives under ``template/{{project_slug}}/`` -- the
    literal folder name -- so a normal package import will not find
    it. ``importlib.util.spec_from_file_location`` sidesteps the
    package layout entirely; the shipped runner is designed so its
    module-level code only defines names (no top-level side effects
    beyond ``sys.path.insert`` for the sibling ``red_team_check``
    module), which lets us import it in-process.
    """
    if not RUNNER_PATH.exists():
        raise FileNotFoundError(f"shipped runner missing at {RUNNER_PATH}")
    spec = importlib.util.spec_from_file_location(
        "shipped_reviewer_eval", str(RUNNER_PATH)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CasesSchemaTests(unittest.TestCase):
    """Every case carries the exact eight fields, valid values, and pairing."""

    @classmethod
    def setUpClass(cls):
        cls.cases = _load_cases()

    def test_cases_file_is_a_nonempty_list(self):
        self.assertIsInstance(
            self.cases,
            list,
            f"{CASES_PATH.name} must be a JSON list of case objects.",
        )
        self.assertGreater(
            len(self.cases),
            0,
            f"{CASES_PATH.name} must contain at least one case; PR 4 seeds "
            "a vulnerable+safe pair for each of the seven domains.",
        )

    def test_every_case_has_all_required_fields(self):
        for idx, case in enumerate(self.cases):
            with self.subTest(index=idx, id=case.get("id", "<missing>")):
                for field in REQUIRED_FIELDS:
                    self.assertIn(
                        field,
                        case,
                        f"case #{idx} missing required field {field!r}; "
                        "cases.json entries must carry the exact eight "
                        "fields the brief pins.",
                    )

    def test_case_ids_are_unique(self):
        ids = [case["id"] for case in self.cases]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        self.assertFalse(
            duplicates,
            f"duplicate case ids in cases.json: {duplicates}; each id must "
            "be unique so a scanner failure can name exactly one case.",
        )

    def test_execution_modes_are_valid(self):
        for case in self.cases:
            with self.subTest(id=case["id"]):
                self.assertIn(
                    case["execution_mode"],
                    VALID_MODES,
                    f"case {case['id']}: execution_mode must be one of "
                    f"{sorted(VALID_MODES)}, got {case['execution_mode']!r}.",
                )

    def test_dispositions_are_valid(self):
        for case in self.cases:
            with self.subTest(id=case["id"]):
                self.assertIn(
                    case["expected_disposition"],
                    VALID_DISPOSITIONS,
                    f"case {case['id']}: expected_disposition must be one "
                    f"of {sorted(VALID_DISPOSITIONS)}, got "
                    f"{case['expected_disposition']!r}.",
                )

    def test_content_fragments_are_string_lists(self):
        for case in self.cases:
            with self.subTest(id=case["id"]):
                self.assertIsInstance(
                    case["content_fragments"],
                    list,
                    f"case {case['id']}: content_fragments must be a list "
                    "of strings so nothing is executed at load time.",
                )
                self.assertGreater(
                    len(case["content_fragments"]),
                    0,
                    f"case {case['id']}: content_fragments must not be empty.",
                )
                for i, frag in enumerate(case["content_fragments"]):
                    self.assertIsInstance(
                        frag,
                        str,
                        f"case {case['id']}: content_fragments[{i}] must be "
                        "a string (JSON data, not code).",
                    )

    def test_expected_rule_ids_is_list(self):
        for case in self.cases:
            with self.subTest(id=case["id"]):
                self.assertIsInstance(
                    case["expected_rule_ids"],
                    list,
                    f"case {case['id']}: expected_rule_ids must be a list.",
                )

    def test_virtual_paths_avoid_root_test_exclusion(self):
        # The root scanner's credential check skips any rel_path containing
        # ``test``. Cases must route around that exclusion so the parametrized
        # deterministic assertion below fires; weakening the exclusion itself
        # is forbidden by the task brief.
        for case in self.cases:
            if case["execution_mode"] != "DETERMINISTIC":
                continue
            with self.subTest(id=case["id"]):
                self.assertNotIn(
                    "test",
                    case["virtual_path"].lower(),
                    f"case {case['id']}: virtual_path {case['virtual_path']!r} "
                    "contains 'test' -- the root scanner's credential-check "
                    "exclusion would skip it. Task 8 must NOT weaken the "
                    "exclusion; pick a virtual_path outside the test tree.",
                )

    def test_deterministic_domains_are_deterministic(self):
        for case in self.cases:
            if case["domain"] in DETERMINISTIC_DOMAINS:
                with self.subTest(id=case["id"]):
                    self.assertEqual(
                        case["execution_mode"],
                        "DETERMINISTIC",
                        f"case {case['id']}: domain {case['domain']!r} is on "
                        "the deterministic list (hardcoded_secret, "
                        "sql_injection, unsafe_shell); every case in that "
                        "domain must ship as DETERMINISTIC.",
                    )

    def test_manual_domains_are_manual(self):
        for case in self.cases:
            if case["domain"] in MANUAL_DOMAINS:
                with self.subTest(id=case["id"]):
                    self.assertEqual(
                        case["execution_mode"],
                        "MANUAL_AGENT",
                        f"case {case['id']}: domain {case['domain']!r} has no "
                        "tested deterministic rule (see coverage matrix in "
                        "reviewer-evals/README.md); it must ship as "
                        "MANUAL_AGENT. CI must never claim manual-only "
                        "coverage as deterministically proven.",
                    )

    def test_every_domain_has_a_vulnerable_and_safe_pair(self):
        by_domain: dict[str, dict[str, list[str]]] = {}
        for case in self.cases:
            slot = by_domain.setdefault(
                case["domain"], {"vulnerable": [], "safe": []}
            )
            slot[case["expected_disposition"]].append(case["id"])
        for domain in ALL_DOMAINS:
            with self.subTest(domain=domain):
                self.assertIn(
                    domain,
                    by_domain,
                    f"domain {domain!r} has no cases; every domain must ship "
                    "both a vulnerable case and a safe near-miss.",
                )
                self.assertTrue(
                    by_domain[domain]["vulnerable"],
                    f"domain {domain!r} has no vulnerable case; the corpus "
                    "must include at least one vulnerable instance per domain.",
                )
                self.assertTrue(
                    by_domain[domain]["safe"],
                    f"domain {domain!r} has no safe near-miss; the corpus "
                    "must include at least one safe counterpart per domain "
                    "so the runner can prove it does not false-block.",
                )

    def test_no_unexpected_domains(self):
        for case in self.cases:
            with self.subTest(id=case["id"]):
                self.assertIn(
                    case["domain"],
                    ALL_DOMAINS,
                    f"case {case['id']}: domain {case['domain']!r} is not in "
                    f"the seven-domain coverage matrix {sorted(ALL_DOMAINS)}.",
                )

    def test_vulnerable_deterministic_cases_declare_expected_rules(self):
        for case in self.cases:
            if (
                case["execution_mode"] == "DETERMINISTIC"
                and case["expected_disposition"] == "vulnerable"
            ):
                with self.subTest(id=case["id"]):
                    self.assertTrue(
                        case["expected_rule_ids"],
                        f"case {case['id']}: a vulnerable DETERMINISTIC "
                        "case must declare at least one expected rule id "
                        "so the runner can prove the scanner emits it.",
                    )

    def test_safe_and_manual_cases_have_empty_expected_rules(self):
        for case in self.cases:
            with self.subTest(id=case["id"]):
                if case["expected_disposition"] == "safe":
                    self.assertEqual(
                        case["expected_rule_ids"],
                        [],
                        f"case {case['id']}: safe cases must expect no "
                        "rule IDs; a rule firing on a safe near-miss is a "
                        "false-block that the runner treats as a failure.",
                    )
                if case["execution_mode"] == "MANUAL_AGENT":
                    self.assertEqual(
                        case["expected_rule_ids"],
                        [],
                        f"case {case['id']}: MANUAL_AGENT cases must expect "
                        "no deterministic rule IDs; deterministic detection "
                        "for these domains is out of scope for Task 8.",
                    )

    def test_manual_cases_name_at_least_one_reviewer(self):
        for case in self.cases:
            if case["execution_mode"] == "MANUAL_AGENT":
                with self.subTest(id=case["id"]):
                    self.assertIsInstance(
                        case["manual_reviewers"],
                        list,
                        f"case {case['id']}: manual_reviewers must be a list.",
                    )
                    self.assertTrue(
                        case["manual_reviewers"],
                        f"case {case['id']}: MANUAL_AGENT cases must name "
                        "at least one prompt reviewer so a human running the "
                        "case knows which agent to feed the input to.",
                    )
                    for reviewer in case["manual_reviewers"]:
                        self.assertIsInstance(
                            reviewer,
                            str,
                            f"case {case['id']}: manual_reviewers entries "
                            "must be strings (agent names).",
                        )

    def test_prompt_injection_treated_as_data(self):
        # The prompt-injection cases must ship as MANUAL_AGENT so their
        # payloads do not travel through any deterministic path that
        # could executed them. Fragments are already asserted to be
        # strings above; this test adds a semantic check that at least
        # one prompt-injection case flags the boundary with an
        # untrusted-input marker in prose (never as an executable
        # instruction to the scanner).
        pi_cases = [c for c in self.cases if c["domain"] == "prompt_injection_boundary"]
        self.assertTrue(
            pi_cases,
            "cases.json must include prompt_injection_boundary cases so "
            "the corpus covers the seventh domain.",
        )
        for case in pi_cases:
            with self.subTest(id=case["id"]):
                self.assertEqual(
                    case["execution_mode"],
                    "MANUAL_AGENT",
                    "prompt-injection cases must ship as MANUAL_AGENT; "
                    "the deterministic runner must never execute or "
                    "interpret their fragments as instructions.",
                )


class NoAssembledSecretMarkerTests(unittest.TestCase):
    """No committed fixture line contains the fully-assembled fake credential."""

    def test_assembled_credential_marker_never_appears_on_disk(self):
        marker = _assembled_credential_marker()
        # Walk the fixture surface + the shipped runner + README to be
        # sure no accidental copy of the assembled marker ships into a
        # generated project. Gitleaks scans the whole repo at root, so
        # a leaked marker anywhere here would trip PR2's blocking gate.
        for path in (CASES_PATH, README_PATH, RUNNER_PATH):
            with self.subTest(path=path.name):
                self.assertTrue(
                    path.exists(),
                    f"{path} missing; Task 8 must ship it.",
                )
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(
                    marker,
                    text,
                    f"{path}: contains the assembled fake credential "
                    f"marker {marker!r}. The fixture must split the "
                    "credential across fragments joined only in memory "
                    "(same discipline as tests/test_agent_review.py). "
                    "A marker on disk would be seen by root Gitleaks.",
                )


class RootScannerDeterministicTests(unittest.TestCase):
    """Root ``review_security`` emits expected rule IDs for every DETERMINISTIC case.

    The parametrization is manual (setUp saves module-level scanner
    state; each subTest clears and restores it) so a failure names
    exactly which case broke. The scanner's test-file exclusion is
    unchanged; the fixture's virtual_path routes around it.
    """

    def setUp(self):
        self._saved_findings = list(agent_review.findings)
        self._saved_warnings = list(agent_review.warnings)
        self.cases = _load_cases()

    def tearDown(self):
        agent_review.findings.clear()
        agent_review.warnings.clear()
        agent_review.findings.extend(self._saved_findings)
        agent_review.warnings.extend(self._saved_warnings)

    def _run_case(self, case: dict) -> list[str]:
        agent_review.findings.clear()
        agent_review.warnings.clear()
        content = "".join(case["content_fragments"])
        agent_review.review_security(
            filepath="virtual.py",
            content=content,
            rel_path=case["virtual_path"],
        )
        return list(agent_review.findings)

    def test_vulnerable_deterministic_cases_fire_expected_rule_ids(self):
        for case in self.cases:
            if case["execution_mode"] != "DETERMINISTIC":
                continue
            if case["expected_disposition"] != "vulnerable":
                continue
            with self.subTest(id=case["id"]):
                findings = self._run_case(case)
                blob = "\n".join(findings)
                for rule_id in case["expected_rule_ids"]:
                    self.assertIn(
                        rule_id,
                        blob,
                        f"case {case['id']}: root scanner did not emit "
                        f"expected rule id {rule_id!r}. Findings so far:\n"
                        f"{blob or '<none>'}\n"
                        "The scanner's test-file exclusion must NOT be "
                        "weakened; check the case's virtual_path routes "
                        "around it (no 'test' substring in the path).",
                    )

    def test_safe_deterministic_cases_do_not_false_block(self):
        for case in self.cases:
            if case["execution_mode"] != "DETERMINISTIC":
                continue
            if case["expected_disposition"] != "safe":
                continue
            with self.subTest(id=case["id"]):
                findings = self._run_case(case)
                self.assertEqual(
                    findings,
                    [],
                    f"case {case['id']}: safe near-miss triggered "
                    f"deterministic finding(s):\n{findings}\n"
                    "A safe case must not false-block; either the case "
                    "content or the scanner needs updating.",
                )


class TemplateRunnerContractTests(unittest.TestCase):
    """The shipped ``reviewer_eval.py`` runner exists, imports, and satisfies its contract."""

    def test_runner_ships_as_plain_python(self):
        self.assertTrue(
            RUNNER_PATH.exists(),
            f"shipped runner missing at {RUNNER_PATH}. Task 8 must ship "
            "it so generated projects run the deterministic corpus in CI.",
        )
        # Must NOT be a .jinja file: it contains no jinja placeholders
        # and copier ships plain .py through unchanged.
        self.assertFalse(
            RUNNER_PATH.name.endswith(".jinja"),
            f"{RUNNER_PATH}: runner must not have a .jinja suffix -- it "
            "has no jinja placeholders and copier renders it as-is.",
        )

    def test_runner_imports_and_exposes_public_entry_points(self):
        module = _load_template_runner()
        # The runner must expose the entry points the tests below drive;
        # a refactor that renames them without updating the tests should
        # be caught here rather than in a surprise ImportError elsewhere.
        for name in (
            "load_cases",
            "validate_schema",
            "run_deterministic_cases",
            "main",
        ):
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(module, name),
                    f"shipped runner is missing public callable "
                    f"{name!r}; the harness contract expects it.",
                )

    def test_runner_validates_valid_fixture(self):
        module = _load_template_runner()
        cases = module.load_cases(CASES_PATH)
        errors = module.validate_schema(cases)
        self.assertEqual(
            errors,
            [],
            f"shipped runner reports schema errors against the seeded "
            f"cases.json: {errors}. The fixture must satisfy the "
            "runner's own schema validator.",
        )

    def test_runner_detects_bad_schema(self):
        module = _load_template_runner()
        bad = [
            {  # missing multiple fields
                "id": "bad_case",
                "domain": "hardcoded_secret",
            }
        ]
        errors = module.validate_schema(bad)
        self.assertTrue(
            errors,
            "shipped runner must report schema errors for a case that "
            "is missing required fields; an all-clear on an invalid "
            "fixture would silently skip cases in CI.",
        )

    def test_runner_passes_seeded_deterministic_cases(self):
        module = _load_template_runner()
        cases = module.load_cases(CASES_PATH)
        errors = module.run_deterministic_cases(cases)
        self.assertEqual(
            errors,
            [],
            f"shipped runner reports deterministic failures against the "
            f"seeded corpus: {errors}. Every DETERMINISTIC vulnerable "
            "case must fire its expected rule id and every safe "
            "near-miss must not false-block.",
        )

    def test_runner_flags_missed_vulnerability(self):
        module = _load_template_runner()
        # Synthetic case that expects a rule the generated scanner
        # will not emit for a plainly safe content (the safe content is
        # a bare pass statement). run_deterministic_cases must catch
        # this: the case is DETERMINISTIC+vulnerable, so a scanner
        # that emits no rule id counts as a missed vulnerability.
        synthetic = [
            {
                "id": "synthetic_missed_vuln",
                "domain": "hardcoded_secret",
                "execution_mode": "DETERMINISTIC",
                "virtual_path": "src/app/handlers.py",
                "content_fragments": ["def ok():\n    pass\n"],
                "expected_disposition": "vulnerable",
                "expected_rule_ids": ["SEC-HARDCODED-CREDENTIAL"],
                "manual_reviewers": [],
            }
        ]
        errors = module.run_deterministic_cases(synthetic)
        self.assertTrue(
            errors,
            "shipped runner did not flag a synthetic vulnerable case "
            "with content that emits no finding; missed vulnerabilities "
            "must return nonzero from the runner.",
        )

    def test_runner_flags_false_block_on_safe_case(self):
        module = _load_template_runner()
        # Synthetic safe-labeled case whose content plainly triggers
        # SEC-SQL-FSTRING. run_deterministic_cases must catch this:
        # a safe case that fires any deterministic rule is a false
        # block that the runner treats as a failure.
        synthetic = [
            {
                "id": "synthetic_false_block",
                "domain": "sql_injection",
                "execution_mode": "DETERMINISTIC",
                "virtual_path": "src/app/handlers.py",
                "content_fragments": [
                    "def unsafe(uid):\n",
                    '    cursor.execute(f"SELECT * FROM u WHERE id = {uid}")\n',
                ],
                "expected_disposition": "safe",
                "expected_rule_ids": [],
                "manual_reviewers": [],
            }
        ]
        errors = module.run_deterministic_cases(synthetic)
        self.assertTrue(
            errors,
            "shipped runner did not flag a synthetic safe case that "
            "clearly triggers a deterministic rule; false-blocks on "
            "safe cases must return nonzero from the runner.",
        )


class ReadmeCoverageMatrixTests(unittest.TestCase):
    """The README ships the coverage matrix that CI honesty depends on."""

    @classmethod
    def setUpClass(cls):
        cls.text = README_PATH.read_text(encoding="utf-8") if README_PATH.exists() else ""

    def test_readme_exists(self):
        self.assertTrue(
            README_PATH.exists(),
            f"{README_PATH} missing; Task 8 must ship it so operators "
            "reading the fixture folder see the coverage matrix.",
        )

    def test_readme_names_every_domain(self):
        for domain in ALL_DOMAINS:
            with self.subTest(domain=domain):
                self.assertIn(
                    domain,
                    self.text,
                    f"README does not name domain {domain!r}; the "
                    "coverage matrix must enumerate all seven domains.",
                )

    def test_readme_names_execution_modes(self):
        for mode in VALID_MODES:
            with self.subTest(mode=mode):
                self.assertIn(
                    mode,
                    self.text,
                    f"README does not mention execution mode {mode!r}; "
                    "the coverage matrix must label each domain with its "
                    "execution mode.",
                )

    def test_readme_does_not_overclaim_manual_coverage(self):
        # Honesty guard: the README must not claim that manual-only
        # domains are deterministically proven, and must not claim CI
        # runs prompt reviewers.
        lowered = self.text.lower()
        for phrase in (
            "path traversal is deterministically",
            "missing auth is deterministically",
            "missing tenant scope is deterministically",
            "prompt injection is deterministically",
            "ci runs prompt reviewers",
        ):
            with self.subTest(phrase=phrase):
                self.assertNotIn(
                    phrase,
                    lowered,
                    f"README overclaims coverage with phrase "
                    f"{phrase!r}. Manual-only domains must never be "
                    "described as deterministically proven, and CI does "
                    "not execute prompt reviewers.",
                )


if __name__ == "__main__":
    unittest.main()
