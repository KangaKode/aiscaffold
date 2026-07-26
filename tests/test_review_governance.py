"""Failing-contract tests for review governance, evidence contracts,
reviewer authority boundaries, and reviewer-assurance consultation (Task 5,
PR 3, TDD).

These tests pin the contract Task 6 must satisfy and Task 7 must anchor:

- ``docs/DEVELOPMENT_PROCESS.md`` and its template counterpart classify every
  confirmed review, CI, or incident finding as either a one-off defect or a
  recurring bug class, and require the three-artifact completion gate
  (regression test + relevant instruction/rule update + register entry)
  before a recurring-class fix may receive final approval.
- Both process docs and both always-applied development-process rules
  reference ``docs/BUG_CLASS_REGISTER.md`` (root and generated counterpart)
  so the closed-loop obligation has a durable destination.
- Task 7 creates the registers themselves. Task 5 asserts their existence
  from the outside.
- ``template/{{project_slug}}/.cursor/rules/expert-review.mdc`` defines a
  shared blocking-evidence contract: security findings require *location*,
  *execution/exploit path* (attacker-controlled source to dangerous sink),
  *trigger/reproduction*, *defense challenge*, *impact*, and *remediation*;
  correctness findings require a failing execution path or invariant plus
  reproducible evidence; and ``UNVERIFIED`` is defined as a non-blocking
  label.
- Every scoped reviewer agent under
  ``template/{{project_slug}}/.cursor/agents/`` -- ``red-team.md``,
  ``sast-reviewer.md``, ``security-hardener.md``,
  ``agent-security-specialist.md``, ``code-reviewer.md``,
  ``solution-architect.md``, ``test-architect.md``, and
  ``data-flow-guardian.md`` -- references that shared contract rather than
  inventing a weaker one.
- Every scoped reviewer explicitly denies each of the four forbidden
  authorities: merge, fix, self-edit-of-own-rules, and self-promotion. The
  denial appears in the reviewer's own text so an operator reading only
  that file still sees the boundary.
- The always-applied red-team rule at
  ``template/{{project_slug}}/.cursor/rules/red-team.mdc`` states that
  analysis always runs, references ``docs/REVIEWER_ASSURANCE.md``, and
  gates a blocking recommendation on the reviewer version being recorded
  as ``BLOCKING`` in that register. Task 6 ships the register as a
  minimal stub listing existing prompt reviewers as ``SHADOW``.

What these tests prove:

- The blocking-evidence contract, closed-loop bug-class register, and
  always-applied consultation clause exist in every asset that must
  carry them.
- Scoped reviewer definitions do not silently claim merge, fix,
  self-edit, or self-promotion authority.

What they do NOT prove:

- That individual review runs actually produce compliant evidence at
  runtime -- that is a manual reviewer-assurance evaluation (Task 6
  bootstraps the register; PR 4 expands the promotion protocol).
- That the initial register contents are correct. Task 7 seeds the
  bug-class register; PR 4 curates the reviewer-assurance register
  beyond the minimal shadow-start stub.
- Historical bug-class content. These tests only pin the meta-contract;
  they do not invent bug classes.

The tests are documentation-parity checks. They will fail today because
the contract, registers, and stable-ID plumbing do not yet exist; they
must pass after Task 6 and Task 7 land.
"""

from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPO_ROOT / "template" / "{{project_slug}}"

ROOT_PROCESS = REPO_ROOT / "docs" / "DEVELOPMENT_PROCESS.md"
TEMPLATE_PROCESS = TEMPLATE_ROOT / "docs" / "DEVELOPMENT_PROCESS.md"
ROOT_DEV_RULE = REPO_ROOT / ".cursor" / "rules" / "development-process.mdc"
TEMPLATE_DEV_RULE = TEMPLATE_ROOT / ".cursor" / "rules" / "development-process.mdc"

PROCESS_ASSETS = {
    "root_process": ROOT_PROCESS,
    "template_process": TEMPLATE_PROCESS,
}
DEV_RULE_ASSETS = {
    "root_dev_rule": ROOT_DEV_RULE,
    "template_dev_rule": TEMPLATE_DEV_RULE,
}
ALL_GOVERNANCE_ASSETS = {**PROCESS_ASSETS, **DEV_RULE_ASSETS}

ROOT_BUG_REGISTER = REPO_ROOT / "docs" / "BUG_CLASS_REGISTER.md"
TEMPLATE_BUG_REGISTER = TEMPLATE_ROOT / "docs" / "BUG_CLASS_REGISTER.md"
ROOT_REVIEWER_ASSURANCE = REPO_ROOT / "docs" / "REVIEWER_ASSURANCE.md"
TEMPLATE_REVIEWER_ASSURANCE = TEMPLATE_ROOT / "docs" / "REVIEWER_ASSURANCE.md"

TEMPLATE_EXPERT_REVIEW = TEMPLATE_ROOT / ".cursor" / "rules" / "expert-review.mdc"
TEMPLATE_RED_TEAM_RULE = TEMPLATE_ROOT / ".cursor" / "rules" / "red-team.mdc"

TEMPLATE_AGENTS_DIR = TEMPLATE_ROOT / ".cursor" / "agents"
SCOPED_REVIEWERS = (
    "red-team.md",
    "sast-reviewer.md",
    "security-hardener.md",
    "agent-security-specialist.md",
    "code-reviewer.md",
    "solution-architect.md",
    "test-architect.md",
    "data-flow-guardian.md",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Bug-class governance helpers
# ---------------------------------------------------------------------------


_BUG_CLASS_ANCHOR_RE = re.compile(
    # Both orderings ("one-off ... class" and "class ... one-off") are valid.
    r"one[- ]off[^\n]{0,80}(?:versus|vs\.?|or|nor|and)[^\n]{0,40}"
    r"(?:recurring|class)"
    r"|(?:recurring|class)[^\n]{0,80}(?:versus|vs\.?|or|nor|and)[^\n]{0,40}"
    r"one[- ]off",
    re.IGNORECASE,
)


def _bug_class_window(text: str) -> str | None:
    """Return the slice of ``text`` around the one-off-vs-class clause.

    Anchoring on the classification phrase (rather than a bare
    ``recurring`` or ``class``) is deliberate: both words appear
    unrelatedly elsewhere in the process docs (e.g. ``recurring
    payment`` in eval fixtures, ``class`` as a Python keyword). The
    forward window stops at the next markdown ``##`` heading so a later
    section cannot supply the completion-gate wording vacuously.
    """
    match = _BUG_CLASS_ANCHOR_RE.search(text)
    if match is None:
        return None
    start = max(0, match.start() - 120)
    after = text[match.end() :]
    next_heading = re.search(r"(?m)^##[\s#]", after)
    if next_heading is not None:
        end = match.end() + next_heading.start()
    else:
        end = match.end() + 1500
    return text[start:end]


# ---------------------------------------------------------------------------
# Evidence-contract helpers
# ---------------------------------------------------------------------------


_PROOF_HEADING_RE = re.compile(
    r"(?im)^#+\s+(?:proof[- ]of[- ]finding"
    r"|blocking[- ]evidence(?:\s+contract)?"
    r"|evidence\s+contract"
    r"|finding\s+evidence)\b"
)


def _proof_of_finding_window(text: str) -> str | None:
    """Return the section body under the proof-of-finding heading.

    The contract must live under a named section so the reviewer agents
    that reference ``expert-review`` can point to one anchor. A
    file-wide substring match would let e.g. the word ``location`` in an
    unrelated ``Location of results`` paragraph satisfy the check; a
    section-scoped window is the same anti-vacuous pattern used by
    ``low_obligation_window`` in ``tests/test_development_process.py``.
    """
    match = _PROOF_HEADING_RE.search(text)
    if match is None:
        return None
    after = text[match.end() :]
    next_heading = re.search(r"(?m)^##[\s#]", after)
    end = len(after) if next_heading is None else next_heading.start()
    return after[:end]


# The six mandatory fields Task 6 must pin as required for a blocking
# security finding, plus the accepted phrasings Task 6 may use for each.
# Task 6 is free to name the section ``Proof of Finding`` or ``Blocking
# Evidence Contract`` (or similar) so long as the required fields appear
# inside the section body.
_SECURITY_FIELD_PATTERNS = {
    "location": re.compile(
        r"(?im)\b(?:exact\s+)?location\b",
    ),
    "execution or exploit path (source to sink)": re.compile(
        r"(?i)(?:execution|exploit)[- ]path"
        r"|source\s*(?:→|->|to)\s*sink"
        r"|attacker[- ]controlled\s+source"
    ),
    "trigger or reproduction": re.compile(
        r"(?i)\btrigger\b|\brepro(?:duction|)\b",
    ),
    "defense challenge": re.compile(
        r"(?i)challenge[^.\n]{0,60}defense"
        r"|defense[^.\n]{0,60}challenge"
        r"|challenge\s+(?:against|to)\s+(?:existing\s+)?defenses?",
    ),
    "impact": re.compile(r"(?i)\bimpact\b"),
    "remediation": re.compile(r"(?i)\bremediation\b"),
}


# ---------------------------------------------------------------------------
# Reviewer authority helpers
# ---------------------------------------------------------------------------


# The four forbidden authorities. Task 6 must add explicit denial language
# to each scoped reviewer agent. Each concept is matched loosely so Task 6
# can phrase the denial in prose ("This reviewer has no merge, fix,
# self-edit-of-own-rules, or self-promotion authority.") or as a list.
_FORBIDDEN_AUTHORITIES = {
    "merge": re.compile(
        r"no\s+merge\s+authority"
        r"|(?:not|never|no|cannot|do\s+not|does\s+not)[^.\n]{0,60}"
        r"\bmerges?\b",
        re.IGNORECASE,
    ),
    "fix": re.compile(
        r"no\s+fix\s+authority"
        r"|(?:not|never|no|cannot|do\s+not|does\s+not)[^.\n]{0,60}"
        r"(?:apply\s+fix|apply\s+the\s+fix|apply\s+fixes|"
        r"fix\s+code|write\s+the\s+fix)",
        re.IGNORECASE,
    ),
    "self-edit-of-own-rules": re.compile(
        r"no\s+self[- ]edit(?:ing)?(?:[- ]of)?[- ]own[- ]rules?"
        r"|(?:not|never|no|cannot|do\s+not|does\s+not)[^.\n]{0,80}"
        r"edit[^.\n]{0,40}"
        r"(?:its?\s+own|their\s+own|the\s+reviewer['\u2019]s\s+own|own)\s+rules?",
        re.IGNORECASE,
    ),
    "self-promotion": re.compile(
        r"no\s+self[- ]promotion"
        r"|(?:not|never|no|cannot|do\s+not|does\s+not)[^.\n]{0,60}"
        r"self[- ]promot(?:e|ion|ing)",
        re.IGNORECASE,
    ),
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class BugClassGovernanceTests(unittest.TestCase):
    """Root and template governance surfaces encode the closed-loop bug-class contract.

    Iterates the four governance surfaces (both process docs and both
    always-applied dev-process rules) via ``subTest`` so a failure names
    both the missing element and the missing asset.
    """

    def test_process_docs_classify_finding_as_one_off_or_class(self):
        for name, path in PROCESS_ASSETS.items():
            with self.subTest(asset=name):
                text = _text(path)
                window = _bug_class_window(text)
                self.assertIsNotNone(
                    window,
                    f"{name}: no 'one-off vs recurring class' classification "
                    "clause found. Task 6 must document that every confirmed "
                    "review, CI, or incident finding is classified as either "
                    "a one-off defect or a recurring bug class.",
                )
                self.assertRegex(
                    window,
                    r"(?i)recurring",
                    f"{name}: bug-class clause matched an anchor but lacks "
                    "the word 'recurring' inside the surrounding section; "
                    "Task 6 must state the two-branch classification "
                    "explicitly, not by implication.",
                )
                self.assertRegex(
                    window,
                    r"(?i)one[- ]off",
                    f"{name}: bug-class clause matched an anchor but lacks "
                    "the phrase 'one-off' inside the surrounding section; "
                    "Task 6 must name both branches (one-off *and* "
                    "recurring class).",
                )

    def test_process_docs_require_three_artifact_completion(self):
        # A recurring bug-class fix must ship, in the same PR, all three of:
        # (1) a regression test failing on the pre-fix code;
        # (2) an update to the relevant agent rule or instruction;
        # (3) an entry in the bug-class register.
        # A file-wide substring match would let each concept sit in an
        # unrelated section; scope every signal to the bug-class window so
        # the completion gate is proven to co-occur with the classification.
        required_signals = {
            "regression test": re.compile(r"regression\s+test", re.IGNORECASE),
            "rule or instruction update": re.compile(
                r"(?:relevant\s+)?(?:rule|instruction|agent\s+rule|agent\s+instruction)"
                r"[^\n]{0,60}update"
                r"|update[^\n]{0,60}(?:rule|instruction|agent\s+rule|agent\s+instruction)",
                re.IGNORECASE,
            ),
            "register entry": re.compile(
                r"(?:BUG_CLASS_REGISTER|audit[- ]register|bug[- ]class\s+register|register)"
                r"[^\n]{0,60}entr"
                r"|entr[a-z]*[^\n]{0,60}"
                r"(?:BUG_CLASS_REGISTER|audit[- ]register|bug[- ]class\s+register|register)",
                re.IGNORECASE,
            ),
        }
        for name, path in PROCESS_ASSETS.items():
            with self.subTest(asset=name):
                text = _text(path)
                window = _bug_class_window(text)
                self.assertIsNotNone(
                    window,
                    f"{name}: no bug-class classification clause to scope "
                    "the three-artifact completion gate against. Fix the "
                    "'classify finding' test first.",
                )
                for signal, pattern in required_signals.items():
                    with self.subTest(asset=name, signal=signal):
                        self.assertRegex(
                            window,
                            pattern,
                            f"{name}: closed-loop completion gate missing "
                            f"'{signal}' inside the bug-class section. "
                            "Task 6 must document that a recurring bug-class "
                            "fix requires (a) a regression test failing on "
                            "the pre-fix code, (b) a relevant agent-rule / "
                            "instruction update, and (c) a "
                            "docs/BUG_CLASS_REGISTER.md entry linking the "
                            "source finding, prevention rule, and regression "
                            "test.",
                        )

    def test_governance_assets_reference_bug_class_register_path(self):
        # All four governance assets (both process docs, both dev rules)
        # must reference the register path so the always-applied surface
        # points to the closed-loop obligation.
        for name, path in ALL_GOVERNANCE_ASSETS.items():
            with self.subTest(asset=name):
                text = _text(path)
                self.assertIn(
                    "BUG_CLASS_REGISTER.md",
                    text,
                    f"{name}: does not reference docs/BUG_CLASS_REGISTER.md. "
                    "Task 6 must add the register path so the always-applied "
                    "rule and the process doc both name where recurring "
                    "bug-class entries live. Task 7 creates the register "
                    "file itself.",
                )

    def test_root_bug_class_register_exists(self):
        self.assertTrue(
            ROOT_BUG_REGISTER.exists(),
            f"Root bug-class register missing at {ROOT_BUG_REGISTER}. Task 7 "
            "creates it so recurring-class fixes have a real audit "
            "destination in the root repository.",
        )

    def test_template_bug_class_register_exists(self):
        self.assertTrue(
            TEMPLATE_BUG_REGISTER.exists(),
            f"Template bug-class register missing at "
            f"{TEMPLATE_BUG_REGISTER}. Task 7 creates the template "
            "counterpart so generated projects inherit the contract and do "
            "not start in a shadow dead-end.",
        )


class BugClassAnchorRegressionTests(unittest.TestCase):
    """Regression: bug-class anchor must not fire on unrelated ``recurring``.

    Bugbot-style guard: the anchor regex targets an ordered pair
    (one-off / recurring, or recurring / one-off). A bare ``recurring``
    in an unrelated section (e.g. ``recurring payment`` in an eval
    fixture or ``recurring flag`` in operations docs) must not silently
    supply the classification clause.
    """

    def test_bare_recurring_word_is_not_a_bug_class_clause(self):
        synthetic = (
            "## Payments\n"
            "Handle a recurring payment safely.\n"
            "## Unrelated\n"
        )
        self.assertIsNone(
            _bug_class_window(synthetic),
            "bug-class anchor false-fires on a bare 'recurring' mention; "
            "it must require the ordered pair with 'one-off' nearby.",
        )

    def test_one_off_vs_recurring_class_is_matched(self):
        synthetic = (
            "## Closed-Loop Feedback\n"
            "Classify every finding as either a one-off defect or a "
            "recurring bug class.\n"
            "A recurring class is not complete until the same PR ships a "
            "regression test, an update to the relevant rule, and an entry "
            "in the audit register.\n"
        )
        window = _bug_class_window(synthetic)
        self.assertIsNotNone(window)
        self.assertIn("regression test", window)
        self.assertIn("register", window)


class EvidenceContractTests(unittest.TestCase):
    """The shared blocking-evidence contract lives in expert-review.mdc.

    A blocking security finding requires all six fields (location,
    execution/exploit path, trigger/reproduction, defense challenge,
    impact, and remediation). A correctness finding requires a failing
    execution path or invariant plus reproducible evidence. UNVERIFIED
    is defined as a non-blocking label. Every scoped reviewer references
    that contract rather than inventing a weaker one.
    """

    def test_expert_review_has_proof_of_finding_section(self):
        self.assertTrue(
            TEMPLATE_EXPERT_REVIEW.exists(),
            f"expert-review rule missing at {TEMPLATE_EXPERT_REVIEW}; "
            "Task 6 must add the shared blocking-evidence contract there.",
        )
        text = _text(TEMPLATE_EXPERT_REVIEW)
        self.assertIsNotNone(
            _proof_of_finding_window(text),
            "expert-review.mdc has no proof-of-finding / blocking-evidence "
            "section heading. Task 6 must add a ``## Proof of Finding`` "
            "(or ``## Blocking Evidence Contract``) section so scoped "
            "reviewers have a single anchor to reference. Today the file "
            "only names Severity/File:Line/Description/Fix, which is not "
            "the full contract.",
        )

    def test_expert_review_security_finding_fields_present(self):
        text = _text(TEMPLATE_EXPERT_REVIEW)
        window = _proof_of_finding_window(text)
        self.assertIsNotNone(
            window,
            "no proof-of-finding section to scope security-field checks "
            "against. Fix the 'proof-of-finding section' test first.",
        )
        for field, pattern in _SECURITY_FIELD_PATTERNS.items():
            with self.subTest(field=field):
                self.assertRegex(
                    window,
                    pattern,
                    f"expert-review proof-of-finding section is missing the "
                    f"'{field}' element required for a blocking security "
                    "finding. Task 6 must state that every blocking "
                    "security finding includes location, execution/exploit "
                    "path (attacker-controlled source to dangerous sink), "
                    "trigger or reproduction, challenge against existing "
                    "defenses, impact, and specific remediation.",
                )

    def test_expert_review_correctness_finding_contract_present(self):
        text = _text(TEMPLATE_EXPERT_REVIEW)
        window = _proof_of_finding_window(text) or ""
        # A blocking correctness finding requires a failing execution path
        # or invariant AND reproducible evidence. Both must appear.
        self.assertRegex(
            window,
            r"(?i)(?:failing\s+)?(?:execution\s+path|invariant)",
            "expert-review proof-of-finding section is missing the "
            "correctness-finding contract: a blocking non-security finding "
            "must identify the failing execution path or violated invariant. "
            "Task 6 must add this branch of the contract.",
        )
        self.assertRegex(
            window,
            r"(?i)reproducible\s+evidence|reproduc(?:e|ible|tion)",
            "expert-review proof-of-finding section is missing the "
            "'reproducible evidence' requirement for correctness findings. "
            "Task 6 must state that a blocking correctness finding requires "
            "reproducible evidence, not just an assertion.",
        )

    def test_expert_review_defines_unverified_as_non_blocking(self):
        text = _text(TEMPLATE_EXPERT_REVIEW)
        self.assertIn(
            "UNVERIFIED",
            text,
            "expert-review.mdc does not use the literal label ``UNVERIFIED``. "
            "Task 6 must add UNVERIFIED as the label for a finding that "
            "cannot meet the proof-of-finding standard: it may be reported "
            "for follow-up but must not block or count toward a clean-slate "
            "target.",
        )
        # UNVERIFIED must be linked to a non-blocking / follow-up disposition
        # in the same clause -- otherwise Task 6 could add the literal word
        # without wiring the semantics.
        unverified_disposition = re.compile(
            r"UNVERIFIED[^\n]{0,200}(?:non[- ]blocking|cannot\s+block|"
            r"does\s+not\s+block|follow[- ]up|not\s+a\s+block)"
            r"|(?:non[- ]blocking|cannot\s+block|does\s+not\s+block|"
            r"follow[- ]up)[^\n]{0,200}UNVERIFIED",
            re.IGNORECASE | re.DOTALL,
        )
        self.assertRegex(
            text,
            unverified_disposition,
            "expert-review.mdc mentions UNVERIFIED but does not tie it to "
            "non-blocking disposition. Task 6 must state UNVERIFIED "
            "explicitly is non-blocking (report for follow-up, does not "
            "count toward a clean-slate target).",
        )

    def test_each_scoped_reviewer_references_shared_contract(self):
        # Reviewers must point at ``expert-review`` (the rule name / file)
        # rather than invent an independent finding schema. The reference
        # can be a link, a filename, or the phrase ``expert-review`` in
        # prose. Task 6 must add the reference to each of the eight
        # scoped agent files.
        for name in SCOPED_REVIEWERS:
            path = TEMPLATE_AGENTS_DIR / name
            with self.subTest(agent=name):
                self.assertTrue(
                    path.exists(),
                    f"scoped reviewer definition missing at {path}; "
                    "Task 6 cannot update a file that does not exist.",
                )
                text = _text(path)
                self.assertRegex(
                    text,
                    r"(?i)expert[- ]review",
                    f"{name}: does not reference the shared "
                    "``expert-review`` blocking-evidence contract. Task 6 "
                    "must add a reference so this reviewer does not invent "
                    "a weaker finding schema. A single link or explicit "
                    "``see expert-review.mdc for the blocking-evidence "
                    "contract`` phrase is enough.",
                )


class ReviewerAuthorityTests(unittest.TestCase):
    """Scoped reviewers must never grant themselves forbidden authorities.

    Each of the eight scoped reviewer definitions must state that this
    reviewer has no merge, fix, self-edit-of-own-rules, or self-promotion
    authority. The denial appears in the reviewer's own file so an
    operator reading only that file still sees the boundary.
    """

    def test_each_scoped_reviewer_denies_forbidden_authorities(self):
        for name in SCOPED_REVIEWERS:
            path = TEMPLATE_AGENTS_DIR / name
            with self.subTest(agent=name):
                self.assertTrue(
                    path.exists(),
                    f"scoped reviewer definition missing at {path}.",
                )
                text = _text(path)
                for authority, pattern in _FORBIDDEN_AUTHORITIES.items():
                    with self.subTest(agent=name, authority=authority):
                        self.assertRegex(
                            text,
                            pattern,
                            f"{name}: does not explicitly deny "
                            f"{authority!r} authority. Task 6 must add "
                            "explicit language (e.g. 'This reviewer has no "
                            "merge, fix, self-edit-of-own-rules, or "
                            "self-promotion authority.') so no scoped "
                            "reviewer can silently claim it. Existing "
                            "phrases like 'you do not modify code' are "
                            "insufficient -- they do not cover merge, "
                            "self-edit of rules, or self-promotion.",
                        )


class AlwaysAppliedReviewerConsultationTests(unittest.TestCase):
    """Always-applied reviewer rules consult the reviewer-assurance register.

    Analysis always runs, but a blocking recommendation is allowed only
    when this reviewer version is recorded as ``BLOCKING`` in
    ``docs/REVIEWER_ASSURANCE.md``. Task 6 ships that register as a
    minimal stub listing existing prompt reviewers as ``SHADOW``.
    """

    def test_always_applied_red_team_rule_is_still_always_applied(self):
        self.assertTrue(
            TEMPLATE_RED_TEAM_RULE.exists(),
            f"always-applied red-team rule missing at "
            f"{TEMPLATE_RED_TEAM_RULE}. Task 6 must update it in place, not "
            "delete it.",
        )
        text = _text(TEMPLATE_RED_TEAM_RULE)
        self.assertRegex(
            text,
            r"(?m)^alwaysApply:\s*true\s*$",
            "template red-team.mdc must retain ``alwaysApply: true`` "
            "frontmatter after Task 6's edits -- consulting the assurance "
            "register only matters if the rule fires on every session.",
        )

    def test_always_applied_red_team_rule_states_analysis_always_runs(self):
        text = _text(TEMPLATE_RED_TEAM_RULE)
        self.assertRegex(
            text,
            r"(?is)analysis\s+always\s+runs"
            r"|always\s+run\s+analysis"
            r"|always\s+analyze"
            r"|analysis\s+runs\s+on\s+every",
            "always-applied red-team rule does not state that analysis "
            "always runs. Task 6 must add explicit language so it is clear "
            "that the reviewer's *analysis* is unconditional even when its "
            "blocking authority is gated by REVIEWER_ASSURANCE.md.",
        )

    def test_always_applied_red_team_rule_references_reviewer_assurance_register(self):
        text = _text(TEMPLATE_RED_TEAM_RULE)
        self.assertIn(
            "REVIEWER_ASSURANCE.md",
            text,
            "always-applied red-team rule does not reference "
            "docs/REVIEWER_ASSURANCE.md. Task 6 must add the register path "
            "so the consultation contract is discoverable inside the rule "
            "itself; the register file itself ships as a minimal stub in "
            "the same task.",
        )

    def test_always_applied_red_team_rule_gates_blocking_on_blocking_status(self):
        text = _text(TEMPLATE_RED_TEAM_RULE)
        # The rule must state that a blocking *recommendation* is allowed
        # only when this reviewer version is recorded as ``BLOCKING`` in
        # the assurance register. A bare mention of the register elsewhere
        # in the file must not satisfy this check -- the two concepts must
        # co-occur in a single window.
        blocking_context_re = re.compile(
            r"`?BLOCKING`?[^\n]{0,120}"
            r"(?:recommend|status|allowed|record|only|reviewer)",
            re.IGNORECASE | re.DOTALL,
        )
        only_gate_re = re.compile(
            r"(?:only|allowed|may|permitted)"
            r"[^\n]{0,80}`?BLOCKING`?"
            r"|`?BLOCKING`?[^\n]{0,80}"
            r"(?:only|allowed|may|permitted)",
            re.IGNORECASE | re.DOTALL,
        )
        window_matches = re.finditer(r"REVIEWER_ASSURANCE\.md", text)
        satisfied = False
        for match in window_matches:
            start = max(0, match.start() - 400)
            end = min(len(text), match.end() + 400)
            window = text[start:end]
            if blocking_context_re.search(window) and only_gate_re.search(window):
                satisfied = True
                break
        self.assertTrue(
            satisfied,
            "always-applied red-team rule does not gate a blocking "
            "recommendation on the reviewer version being recorded as "
            "``BLOCKING`` in docs/REVIEWER_ASSURANCE.md. Task 6 must add a "
            "sentence stating that a blocking recommendation is allowed "
            "*only* when this reviewer version is recorded as BLOCKING in "
            "the assurance register; otherwise the reviewer runs in SHADOW "
            "and may comment but not block.",
        )


class ReviewerAssuranceRegisterTests(unittest.TestCase):
    """Task 6 ships the reviewer-assurance register as a minimal stub.

    Both the root register (used by root-level red-team review) and the
    template register (rendered into every generated project) exist. PR
    4 expands the promotion protocol; Task 5 asserts the file exists so
    the always-applied consultation contract has a real destination.
    """

    def test_root_reviewer_assurance_register_exists(self):
        self.assertTrue(
            ROOT_REVIEWER_ASSURANCE.exists(),
            f"Root reviewer-assurance register missing at "
            f"{ROOT_REVIEWER_ASSURANCE}. Task 6 must create it as a "
            "minimal stub listing existing prompt reviewers as SHADOW so "
            "the consultation contract is real.",
        )

    def test_template_reviewer_assurance_register_exists(self):
        self.assertTrue(
            TEMPLATE_REVIEWER_ASSURANCE.exists(),
            f"Template reviewer-assurance register missing at "
            f"{TEMPLATE_REVIEWER_ASSURANCE}. Task 6 must create the "
            "template counterpart so generated projects inherit the "
            "consultation contract, not start in a shadow dead-end.",
        )


if __name__ == "__main__":
    unittest.main()
