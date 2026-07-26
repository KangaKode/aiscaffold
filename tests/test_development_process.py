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
    satisfied by diagram labels or ``non-Low`` prose.
    """
    lower = text.lower()
    match = _LOW_EXEMPT_RE.search(lower)
    if match is None:
        return None
    start = max(0, match.start() - 200)
    return lower[start : match.start() + 1500]


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
                lower = self._text(name).lower()
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
                medium_anchor = re.search(r"\bmedium\b", lower)
                self.assertIsNotNone(
                    medium_anchor,
                    f"{name}: no bare-word 'medium' anchor to scope the 'concise' "
                    "requirement to Medium-tier text",
                )
                # Scope "concise" to a paragraph-sized window around the Medium
                # anchor so this assertion cannot be satisfied by an unrelated
                # occurrence of the word elsewhere in the doc.
                start = max(0, medium_anchor.start() - 200)
                medium_window = lower[start : medium_anchor.start() + 800]
                self.assertIn(
                    "concise",
                    medium_window,
                    f"{name}: Medium tier design note not required to be 'concise' "
                    "in the paragraph adjacent to the Medium-tier anchor",
                )

    def test_low_tier_exempts_design_artifacts_only(self):
        # Word-boundary / phrase patterns for the Low-tier obligations that must
        # survive even though design artifacts are exempted. Substring matching
        # here would be tautological on the ambient docs: "ci" matches
        # "specific"/"decision", "test" matches "latest", "review" matches
        # "reviewer", and "branch" matches "branches" — none of which prove
        # Low still carries the obligation.
        #
        # Window anchor is the exemption clause itself (not the first bare
        # ``\blow\b``), because process docs put an early "Low" in the mermaid
        # diagram and always-applied rules match "Low" inside "non-Low".
        obligation_patterns = {
            "branch isolation": r"\bbranch(?:es)?\b",
            "CI": r"\bci\b",
            "human ownership": r"human ownership",
            "post-change review": r"\b(?:post[- ]change[- ])?reviews?\b",
            "tests": r"\btests?\b",
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
                    "branch isolation / CI / human ownership / applicable "
                    "review / tests obligations (whole-word matched, "
                    "scoped to the Low exemption clause)",
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
    from the exemption section.
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
            "Branch isolation, CI, tests, human ownership, and "
            "post-change review still apply.\n"
        )
        window = low_obligation_window(synthetic)
        self.assertIsNotNone(window)
        # Exemption-anchored window must include the preserve list.
        self.assertRegex(window, r"\bci\b")
        self.assertIn("human ownership", window)
        # And must NOT be the early diagram slice (which has "Low" but no
        # exemption clause and no preserve list in the first 200 chars before
        # an early-only match). Sanity: exemption text is in the window.
        self.assertIn("exempt", window)
        self.assertIn("design artifact", window)

    def test_first_bare_low_window_would_miss_obligations(self):
        """Demonstrate the pre-fix bug: first \\blow\\b is the wrong anchor."""
        synthetic = (
            'Classify["Risk-Tier (High / Medium / Low)"]\n'
            + ("padding " * 400)
            + "### Low\n"
            "Low-tier work is exempt from design artifacts only. "
            "Branch isolation, CI, tests, human ownership, and "
            "post-change review still apply.\n"
        )
        lower = synthetic.lower()
        first_low = re.search(r"\blow\b", lower)
        self.assertIsNotNone(first_low)
        bad_start = max(0, first_low.start() - 200)
        bad_window = lower[bad_start : first_low.start() + 1500]
        # Pre-fix window around the diagram "Low" never reaches the
        # exemption clause when enough padding separates them.
        self.assertNotIn("exempt", bad_window)
        # Correct helper still finds the exemption section.
        good_window = low_obligation_window(synthetic)
        self.assertIsNotNone(good_window)
        self.assertIn("exempt", good_window)
        self.assertRegex(good_window, r"\bci\b")

    def test_missing_exemption_returns_none(self):
        self.assertIsNone(
            low_obligation_window("High and Medium only. No exemption clause.")
        )


if __name__ == "__main__":
    unittest.main()
