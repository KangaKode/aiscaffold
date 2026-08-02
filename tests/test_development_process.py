"""Documentation-parity tests for the risk-tier development process policy.

These tests read the root and generated (template source) process assets and
assert that the approved High/Medium/Low risk-tier policy is present in each of
the four governance surfaces:

- ``docs/DEVELOPMENT_PROCESS.md`` (root process doc)
- ``.cursor/rules/development-process.mdc`` (root always-applied rule)
- ``template/{{project_slug}}/docs/DEVELOPMENT_PROCESS.md`` (generated process doc)
- ``template/{{project_slug}}/.cursor/rules/development-process.mdc``
  (generated always-applied rule)

They are documentation-parity checks: they prove the policy text exists in every
asset that must carry it, not that humans classify individual changes correctly.
Actual tier assignment and review remain a human gate.
"""

from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]

POLICY_ASSETS = {
    "root_process": REPO_ROOT / "docs" / "DEVELOPMENT_PROCESS.md",
    "root_rule": REPO_ROOT / ".cursor" / "rules" / "development-process.mdc",
    "template_process": (
        REPO_ROOT / "template" / "{{project_slug}}" / "docs" / "DEVELOPMENT_PROCESS.md"
    ),
    "template_rule": (
        REPO_ROOT
        / "template"
        / "{{project_slug}}"
        / ".cursor"
        / "rules"
        / "development-process.mdc"
    ),
}

RULE_ASSETS = ("root_rule", "template_rule")

# Exemption-clause pattern used as the Low-tier window anchor. Must not use the
# first bare ``\blow\b`` — process docs put an early "Low" in the mermaid
# diagram, and always-applied rules match "Low" inside "non-Low".
_LOW_EXEMPT_RE = re.compile(r"exempt[a-z]*[^\n]{0,60}design artifact")


def low_obligation_window(text: str) -> str | None:
    """Return the slice of ``text`` around the Low exemption clause, or None.

    Anchoring on the exemption clause (not the first bare ``low``) is the
    regression fix for the Bugbot finding that obligation checks could be
    satisfied by diagram labels or ``non-Low`` prose. The forward window
    stops at the next markdown heading (or a small cap) so the tier-agnostic
    Gates table that follows the Low section cannot satisfy the preserve-list
    check either.
    """
    lower = text.lower()
    match = _LOW_EXEMPT_RE.search(lower)
    if match is None:
        return None
    start = max(0, match.start() - 120)
    after = lower[match.end() :]
    heading = re.search(r"\n##[\s#]", after)
    if heading is not None:
        end = match.end() + heading.start()
    else:
        end = match.end() + 500
    return lower[start:end]


def medium_concise_window(text: str) -> str | None:
    """Return the slice of ``text`` around the Medium-default clause, or None.

    Anchoring on the exact phrase ``medium is the default`` (not a loose
    ``medium ... default`` match, and not the first bare ``medium``) avoids
    the workflow-diagram label and the adjacent mermaid
    ``Medium: 1 concise design note`` / ``Medium (default)`` nodes satisfying
    the check when the Medium policy paragraph itself has dropped the
    concise-note requirement.
    """
    lower = text.lower()
    match = re.search(r"medium is the default", lower)
    if match is None:
        return None
    start = max(0, match.start() - 80)
    return lower[start : match.end() + 400]


class RiskTierPolicyTests(unittest.TestCase):
    """Assert the risk-tier policy is present in every governance surface.

    Every subtest iterates over the four assets in ``POLICY_ASSETS`` so a
    failure reports both which policy element is missing and which asset is
    missing it, without collapsing multiple failures into a single line.
    """

    @classmethod
    def setUpClass(cls):
        cls.asset_text = {
            name: (path.read_text(encoding="utf-8") if path.exists() else None)
            for name, path in POLICY_ASSETS.items()
        }

    def _text(self, name):
        content = self.asset_text[name]
        if content is None:
            self.fail(
                f"Policy asset {name!r} missing at {POLICY_ASSETS[name]}. "
                "Every root and template development-process asset must exist "
                "and carry the risk-tier policy."
            )
        return content

    def test_all_policy_assets_exist(self):
        for name, path in POLICY_ASSETS.items():
            with self.subTest(asset=name):
                self.assertTrue(
                    path.exists(),
                    f"{name}: policy asset does not exist at {path}",
                )

    def test_dev_process_rules_are_always_applied(self):
        for name in RULE_ASSETS:
            with self.subTest(asset=name):
                text = self._text(name)
                self.assertRegex(
                    text,
                    r"(?m)^alwaysApply:\s*true\s*$",
                    f"{name}: 'alwaysApply: true' frontmatter missing; "
                    "development-process rule must be always-applied",
                )

    def test_all_three_tiers_present(self):
        for name in POLICY_ASSETS:
            with self.subTest(asset=name):
                text = self._text(name)
                lower = text.lower()
                self.assertRegex(
                    lower,
                    r"risk[- ]tier",
                    f"{name}: missing 'risk tier' framing that anchors the policy",
                )
                for tier in ("High", "Medium", "Low"):
                    self.assertRegex(
                        text,
                        rf"\b{tier}\b",
                        f"{name}: {tier!r} tier label missing as a bare word",
                    )

    def test_highest_applicable_tier_wins(self):
        for name in POLICY_ASSETS:
            with self.subTest(asset=name):
                lower = self._text(name).lower()
                self.assertRegex(
                    lower,
                    r"highest applicable[^\n]*tier[^\n]*wins",
                    f"{name}: 'highest applicable tier wins' override clause missing",
                )

    def test_high_tier_requires_four_design_artifacts(self):
        for name in POLICY_ASSETS:
            with self.subTest(asset=name):
                lower = self._text(name).lower()
                self.assertRegex(
                    lower,
                    r"architecture[- ]map",
                    f"{name}: High-tier artifact 'architecture map' missing",
                )
                self.assertRegex(
                    lower,
                    r"data[- ]flow",
                    f"{name}: High-tier artifact 'data flow' missing",
                )
                self.assertRegex(
                    lower,
                    r"(?:workflow[- ]states|wireframes)",
                    f"{name}: High-tier artifact 'workflow states/wireframes' missing",
                )
                self.assertRegex(
                    lower,
                    r"threat[- ]model",
                    f"{name}: High-tier artifact 'threat model' missing",
                )

    def test_medium_is_default_and_requires_one_concise_design_note(self):
        for name in POLICY_ASSETS:
            with self.subTest(asset=name):
                text = self._text(name)
                lower = text.lower()
                self.assertRegex(
                    lower,
                    r"medium[^\n]{0,80}default|default[^\n]{0,80}medium",
                    f"{name}: Medium tier not stated as the default for non-High/non-Low changes",
                )
                self.assertRegex(
                    lower,
                    r"one[^\n]{0,30}note",
                    f"{name}: Medium tier does not require one design note",
                )
                medium_window = medium_concise_window(text)
                self.assertIsNotNone(
                    medium_window,
                    f"{name}: no 'medium is the default' clause to scope the "
                    "'concise' requirement away from the workflow diagram",
                )
                self.assertIn(
                    "concise",
                    medium_window,
                    f"{name}: Medium tier design note not required to be 'concise' "
                    "adjacent to the Medium-default clause",
                )

    def test_low_tier_exempts_design_artifacts_only(self):
        # Exact phrases from the Low preserve-list. Loose patterns like
        # ``\bci\b`` / ``\breviews?\b`` / ``\btests?\b`` also match the
        # tier-agnostic Gates table ("CI validation", "Code review",
        # "Test-first planning") that follows the Low section, so a
        # too-wide window would stay green after the Low bullets were
        # deleted.
        obligation_patterns = {
            "branch isolation": r"branch isolation",
            "human ownership": r"human ownership",
            "post-change review": r"post[- ]change review",
            "applicable tests": r"applicable tests",
            "CI validation": r"\bci\b",
        }
        for name in POLICY_ASSETS:
            with self.subTest(asset=name):
                text = self._text(name)
                low_window = low_obligation_window(text)
                self.assertIsNotNone(
                    low_window,
                    f"{name}: Low tier not described as exempting design artifacts",
                )
                obligations_present = sum(
                    1
                    for pattern in obligation_patterns.values()
                    if re.search(pattern, low_window)
                )
                self.assertGreaterEqual(
                    obligations_present,
                    2,
                    f"{name}: Low tier must still preserve at least two of "
                    "branch isolation / human ownership / post-change review / "
                    "applicable tests / CI obligations (phrase-matched, "
                    "scoped tightly to the Low exemption clause)",
                )

    def test_under_20_line_exemption_semantics(self):
        for name in POLICY_ASSETS:
            with self.subTest(asset=name):
                lower = self._text(name).lower()
                self.assertRegex(
                    lower,
                    r"(?:under|fewer than|less than|below)[- ]?20"
                    r"|20[- ]?(?:line|gross|changed|added)",
                    f"{name}: under-20-line threshold missing or unclear",
                )
                self.assertIn(
                    "additions plus deletions",
                    lower,
                    f"{name}: under-20-line rule must count gross additions plus deletions",
                )
                self.assertRegex(
                    lower,
                    r"exclud[a-z]*[^\n]{0,40}generated",
                    f"{name}: under-20-line rule must exclude mechanically generated artifacts",
                )
                self.assertRegex(
                    lower,
                    r"high[- ](?:tier[- ])?paths?[^\n]{0,80}invariant",
                    f"{name}: under-20-line rule must exclude high-tier paths and invariants",
                )


class LowObligationWindowRegressionTests(unittest.TestCase):
    """Regression: obligation window must not use the first bare ``low``.

    Bugbot found that scoping to the first ``\\blow\\b`` match let the
    workflow-diagram tier label or a ``non-Low`` mention satisfy the
    preserve-list check even after Low-tier obligation language was removed
    from the exemption section. A later finding showed a too-wide forward
    window also let the tier-agnostic Gates table satisfy loose patterns.
    """

    def test_window_anchors_on_exemption_not_first_low(self):
        # Early "Low" in a diagram label, and "non-Low" before the real section.
        # Obligations sit next to the exemption clause only.
        synthetic = (
            'Classify["Risk-Tier (High / Medium / Low)"]\n'
            "non-Low changes get a written plan.\n"
            "### High\n"
            "High requires four artifacts and CI review.\n"
            "### Low\n"
            "Low-tier work is exempt from design artifacts only. "
            "Branch isolation, human ownership, and "
            "post-change review still apply.\n"
            "## Gates\n"
            "| Gate | What It Prevents |\n"
            "| Test-first planning | unverified code |\n"
            "| Code review | maintainability |\n"
            "| CI validation | broken tests |\n"
        )
        window = low_obligation_window(synthetic)
        self.assertIsNotNone(window)
        self.assertIn("branch isolation", window)
        self.assertIn("human ownership", window)
        self.assertIn("exempt", window)
        self.assertIn("design artifact", window)
        # Gates table must stay outside the tight forward window.
        self.assertNotIn("test-first planning", window)

    def test_first_bare_low_window_would_miss_obligations(self):
        """Demonstrate the pre-fix bug: first \\blow\\b is the wrong anchor."""
        synthetic = (
            'Classify["Risk-Tier (High / Medium / Low)"]\n'
            + ("padding " * 400)
            + "### Low\n"
            "Low-tier work is exempt from design artifacts only. "
            "Branch isolation, human ownership, and "
            "post-change review still apply.\n"
        )
        lower = synthetic.lower()
        first_low = re.search(r"\blow\b", lower)
        self.assertIsNotNone(first_low)
        bad_start = max(0, first_low.start() - 200)
        bad_window = lower[bad_start : first_low.start() + 1500]
        self.assertNotIn("exempt", bad_window)
        good_window = low_obligation_window(synthetic)
        self.assertIsNotNone(good_window)
        self.assertIn("exempt", good_window)
        self.assertIn("branch isolation", good_window)

    def test_missing_exemption_returns_none(self):
        self.assertIsNone(
            low_obligation_window("High and Medium only. No exemption clause.")
        )

    def test_wide_window_into_gates_is_rejected(self):
        """Obligations must not be satisfied solely by the Gates table."""
        synthetic = (
            "### Low\n"
            "Low-tier work is exempt from design artifacts only.\n"
            + ("x" * 600)
            + "## Gates\n"
            "| Test-first planning | unverified |\n"
            "| Code review | drift |\n"
            "| CI validation | broken tests |\n"
        )
        window = low_obligation_window(synthetic)
        self.assertIsNotNone(window)
        # Tight window ends before the Gates table.
        self.assertNotIn("test-first planning", window)
        self.assertNotIn("code review", window)


class MediumConciseWindowRegressionTests(unittest.TestCase):
    """Regression: concise check must not use the first bare ``medium``."""

    def test_window_anchors_on_default_clause_not_diagram(self):
        synthetic = (
            'Classify["Risk-Tier (High / Medium / Low)"]\n'
            'Classify -->|"Medium (default)"| MediumDoc["Medium: 1 concise design note"]\n'
            + ("padding " * 200)
            + "### Medium\n"
            "**Medium is the default risk-tier** for non-High/non-Low changes. "
            "It requires **one concise design note** covering architecture.\n"
        )
        window = medium_concise_window(synthetic)
        self.assertIsNotNone(window)
        self.assertIn("concise", window)
        self.assertIn("default", window)
        # First bare "medium" is the diagram label; a window there would
        # hit the mermaid node's "concise" without the policy paragraph.
        lower = synthetic.lower()
        first_medium = re.search(r"\bmedium\b", lower)
        self.assertIsNotNone(first_medium)
        bad_start = max(0, first_medium.start() - 200)
        bad_window = lower[bad_start : first_medium.start() + 800]
        # Diagram-adjacent window has "concise" from the mermaid node.
        self.assertIn("concise", bad_window)
        # But it does not include the policy "medium is the default" body
        # once padding separates them — the helper must use that body.
        self.assertNotIn("covering architecture", bad_window)
        self.assertIn("covering architecture", window)

    def test_missing_default_clause_returns_none(self):
        self.assertIsNone(
            medium_concise_window(
                'Classify["Medium"]\nMediumDoc["Medium: 1 concise design note"]\n'
            )
        )


class BugbotHonestyTemplateTests(unittest.TestCase):
    """Template process assets must not require Bugbot as the review path.

    Generated projects fulfill post-diff review via shipped agents. A naive
    sync that copies root ``Bugbot plus the matching domain expert`` wording
    into the template must fail these pins.
    """

    @classmethod
    def setUpClass(cls):
        cls.template_process = POLICY_ASSETS["template_process"].read_text(
            encoding="utf-8"
        )
        cls.template_rule = POLICY_ASSETS["template_rule"].read_text(encoding="utf-8")
        cls.root_rule = POLICY_ASSETS["root_rule"].read_text(encoding="utf-8")

    def test_template_names_shipped_agents_for_post_diff_review(self):
        for text, label in (
            (self.template_process, "template_process"),
            (self.template_rule, "template_rule"),
        ):
            with self.subTest(asset=label):
                lower = text.lower()
                for agent in ("code-reviewer", "red-team", "sast-reviewer"):
                    self.assertIn(
                        agent,
                        lower,
                        f"{label}: post-diff review must name shipped agent {agent}",
                    )

    def test_template_does_not_require_bugbot_as_fulfillment_path(self):
        forbidden = re.compile(
            r"Bugbot plus the matching domain expert",
            re.IGNORECASE,
        )
        for text, label in (
            (self.template_process, "template_process"),
            (self.template_rule, "template_rule"),
        ):
            with self.subTest(asset=label):
                self.assertIsNone(
                    forbidden.search(text),
                    f"{label}: must not require Bugbot as the review fulfillment "
                    "path (do not sync root Bugbot-required wording into template)",
                )

    def test_template_pins_mdc_does_not_configure_bugbot(self):
        for text, label in (
            (self.template_process, "template_process"),
            (self.template_rule, "template_rule"),
        ):
            with self.subTest(asset=label):
                self.assertRegex(
                    text,
                    r"(?is)\.cursor/rules/\*\.mdc[^\n]{0,80}do\s+\*\*not\*\*\s+configure\s+Bugbot"
                    r"|do\s+\*\*not\*\*\s+configure\s+Bugbot",
                    f"{label}: must pin that .mdc rules do not configure Bugbot",
                )

    def test_template_pins_autofix_off_unless_explicit(self):
        for text, label in (
            (self.template_process, "template_process"),
            (self.template_rule, "template_rule"),
        ):
            with self.subTest(asset=label):
                self.assertRegex(
                    text.lower(),
                    r"autofix[^\n]{0,80}(?:off|stays off)",
                    f"{label}: must pin Autofix off unless explicitly enabled",
                )

    def test_root_may_name_bugbot_as_optional_maintainer_tooling(self):
        self.assertRegex(
            self.root_rule,
            r"(?is)Bugbot.{0,80}optional maintainer tooling",
            "root rule may keep Bugbot with optional-maintainer-tooling parenthetical",
        )


if __name__ == "__main__":
    unittest.main()
