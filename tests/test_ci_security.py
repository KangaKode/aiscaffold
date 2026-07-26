"""Failing-contract tests for CI secret scanning and dependency auditing.

These tests pin the workflow contract Task 4 must satisfy: a dedicated
blocking ``security`` job in both the root ``.github/workflows/validate.yml``
and the template ``template/{{project_slug}}/.github/workflows/ci.yml.jinja``
that runs pinned Gitleaks (with ``fetch-depth: 0``) and ``pip-audit`` through
the audit gate at ``template/{{project_slug}}/scripts/pip_audit_gate.py``,
wired into ``summary.needs`` so a security failure blocks merge.

They also pin the secret-scanning baseline: the three known fake-credential
literals must not appear anywhere under ``template/``; the root
``.gitleaksignore`` must exist and list only commit-scoped fingerprints; and
no Gitleaks allowlist config may ship inside a generated project.

What these tests prove:

- The root and template workflow surfaces have a security stage that runs
  the pinned scanners without bypass flags.
- The summary gate depends on the security stage so a scanner failure
  blocks merge.
- Fake-credential fixture values that render into every generated project
  have been cleaned at the source (Task 3B), so first-run Gitleaks in a
  generated repo will be clean without any shipped suppression.

What they do NOT prove:

- That the ``security`` job actually finds real secrets or advisories at
  runtime (that is Gitleaks and pip-audit at CI time).
- That external GitHub branch-protection settings match the summary
  contract; branch protection is out-of-repository configuration.
- That commits already in Git history contain no real secrets (the root
  ``.gitleaksignore`` covers deliberate fixture history only).

The fake-credential fragments are assembled from split literals so this
test file itself is never a Gitleaks finding once the root scanner (Task
4) is live and scans the full repository including ``tests/``.
"""

from pathlib import Path
import re
import unittest

import yaml
from jinja2 import Environment, StrictUndefined


REPO_ROOT = Path(__file__).resolve().parents[1]

ROOT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"
TEMPLATE_WORKFLOW_JINJA = (
    REPO_ROOT
    / "template"
    / "{{project_slug}}"
    / ".github"
    / "workflows"
    / "ci.yml.jinja"
)
ROOT_GITLEAKSIGNORE = REPO_ROOT / ".gitleaksignore"
TEMPLATE_ROOT = REPO_ROOT / "template" / "{{project_slug}}"
TEMPLATE_AUDIT_GATE = TEMPLATE_ROOT / "scripts" / "pip_audit_gate.py"

# Contract literals from the task brief. Any drift here means Task 4 must
# also be updated.
GITLEAKS_PINNED_SHA = "ff98106e4c7b2bc287b24eaf42907196329070c7"
PIP_AUDIT_PIN = "pip-audit==2.10.1"

# Fake-credential fragments. Concatenated at use so this file's own text
# never contains the literals that Gitleaks flags. The three fragments
# reconstruct: the API-key literal in template test middleware, the
# 16-char hex signing key in template agent-identity tests, and the JWT
# header segment in the injection-defense eval fixture.
_FAKE_API_KEY = "correct-" + "key-" + "12345"
_FAKE_HEX16 = "0123" + "4567" + "89abcdef"
_FAKE_JWT_PREFIX = "eyJhbGciOiJIUzI1NiIsInR5cC" + "I6IkpXVCJ9."
FAKE_CREDENTIAL_LITERALS = (_FAKE_API_KEY, _FAKE_HEX16, _FAKE_JWT_PREFIX)

# Copier answers used to render the template workflow into concrete YAML.
# All optional toggles are ON so every rendered optional-extra path in
# pyproject.toml is exercised by the rendered ``pip install`` line.
RENDER_CONTEXT = {
    "project_name": "Contract Test",
    "project_slug": "contract_test",
    "project_description": "Contract test project",
    "author_name": "Contract Tester",
    "project_type": "web-app",
    "layers": "data,analysis,components",
    "llm_provider": "anthropic",
    "persistence": "sqlite",
    "learning_backend": "postgres",
    "python_version": "3.13",
    "include_evals": True,
    "include_api_gateway": True,
    "include_deployment": True,
    "include_learning": True,
}

# Commit-scoped Gitleaks fingerprint: <40-hex-sha>:<path>:<rule>:<line>.
# No wildcard, no bare path, no rule-wide entry.
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{40}:[^:\s]+:[^:\s]+:\d+$")

# Every expression in Jinja source that Copier would render is either
# ``{{ ... }}`` (Jinja variable) or a workflow-level ``${{ ... }}``. Only
# the latter must be inside ``{% raw %}...{% endraw %}`` so it survives
# Copier rendering into the generated workflow.
_GH_EXPR_RE = re.compile(r"\$\{\{")
_RAW_BLOCK_RE = re.compile(r"\{%\s*raw\s*%\}.*?\{%\s*endraw\s*%\}", re.DOTALL)

# Scanner-bypass patterns forbidden anywhere in the security job. The
# ``--fix`` flag would ask pip-audit to auto-upgrade vulnerable packages
# without human review; ``continue-on-error`` and ``|| true`` would
# swallow scanner failures. ``pip install --upgrade pip`` is a legitimate
# preamble step and is deliberately NOT on this list -- only auto-fix
# flags on scanners are.
_BYPASS_PATTERNS = (
    ("continue-on-error: true", re.compile(r"continue-on-error\s*:\s*true", re.IGNORECASE)),
    ("|| true", re.compile(r"\|\|\s*true\b")),
    ("--fix flag", re.compile(r"(?<!-)--fix\b")),
    ("--auto-fix flag", re.compile(r"--auto[-_]fix\b")),
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _render_template_workflow() -> str:
    """Render ``ci.yml.jinja`` with the standard copier answers."""
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    return env.from_string(_text(TEMPLATE_WORKFLOW_JINJA)).render(**RENDER_CONTEXT)


def _job_step_texts(job: dict) -> list[str]:
    """Return each step's full YAML text (name/uses/with/run) as one string."""
    steps = job.get("steps") or []
    out: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        pieces: list[str] = []
        for key in ("name", "uses", "run"):
            value = step.get(key)
            if value is not None:
                pieces.append(f"{key}: {value}")
        with_map = step.get("with") or {}
        if isinstance(with_map, dict):
            for k, v in with_map.items():
                pieces.append(f"with.{k}: {v}")
        if "continue-on-error" in step:
            pieces.append(f"continue-on-error: {step['continue-on-error']}")
        out.append("\n".join(pieces))
    return out


def _job(workflow: dict, name: str) -> dict | None:
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    if not isinstance(jobs, dict):
        return None
    return jobs.get(name)


class RootWorkflowSecurityJobTests(unittest.TestCase):
    """Root ``validate.yml`` must define a blocking ``security`` job."""

    @classmethod
    def setUpClass(cls):
        cls.raw = _text(ROOT_WORKFLOW)
        cls.workflow = yaml.safe_load(cls.raw)

    def test_security_job_exists(self):
        self.assertIsNotNone(
            _job(self.workflow, "security"),
            "Root .github/workflows/validate.yml must define a top-level "
            "'security' job (Task 4 adds it).",
        )

    def test_security_job_checkout_uses_fetch_depth_zero(self):
        job = _job(self.workflow, "security")
        self.assertIsNotNone(
            job,
            "security job missing; cannot verify fetch-depth on its checkout step.",
        )
        checkout_steps = [
            step
            for step in (job.get("steps") or [])
            if isinstance(step, dict)
            and isinstance(step.get("uses"), str)
            and step["uses"].startswith("actions/checkout@")
        ]
        self.assertTrue(
            checkout_steps,
            "security job must include an actions/checkout step so Gitleaks "
            "can scan history.",
        )
        depths = [
            (step.get("with") or {}).get("fetch-depth") for step in checkout_steps
        ]
        self.assertIn(
            0,
            depths,
            "security job's actions/checkout must set 'fetch-depth: 0' "
            "(Gitleaks requires full history to detect reintroduced secrets).",
        )

    def test_gitleaks_uses_pinned_commit_sha(self):
        job = _job(self.workflow, "security")
        self.assertIsNotNone(
            job,
            "security job missing; cannot verify Gitleaks SHA pin.",
        )
        gitleaks_uses = [
            step.get("uses")
            for step in (job.get("steps") or [])
            if isinstance(step, dict)
            and isinstance(step.get("uses"), str)
            and step["uses"].lower().startswith("gitleaks/gitleaks-action@")
        ]
        self.assertTrue(
            gitleaks_uses,
            "security job must invoke gitleaks/gitleaks-action pinned by SHA.",
        )
        for uses in gitleaks_uses:
            self.assertTrue(
                uses.endswith("@" + GITLEAKS_PINNED_SHA),
                f"gitleaks/gitleaks-action reference {uses!r} must be pinned "
                f"to the full commit SHA {GITLEAKS_PINNED_SHA!r} (no tags, no "
                "branches).",
            )

    def test_pip_audit_version_pin_installed(self):
        job = _job(self.workflow, "security")
        self.assertIsNotNone(
            job,
            "security job missing; cannot verify pip-audit pin.",
        )
        blob = "\n".join(_job_step_texts(job))
        self.assertIn(
            PIP_AUDIT_PIN,
            blob,
            f"Root security job must install {PIP_AUDIT_PIN!r} verbatim so "
            "the auditor version is pinned in CI.",
        )

    def test_audit_gate_invoked_against_core(self):
        job = _job(self.workflow, "security")
        self.assertIsNotNone(
            job,
            "security job missing; cannot verify audit-gate invocation.",
        )
        blob = "\n".join(_job_step_texts(job))
        self.assertIn(
            "pip_audit_gate.py",
            blob,
            "Root security job must invoke the shared audit gate at "
            "template/{{project_slug}}/scripts/pip_audit_gate.py; there is no "
            "second copy at the root.",
        )
        self.assertRegex(
            blob,
            r"\bcore\b",
            "Root security job must scope the audit-gate invocation to "
            "core/ (the root aiscaffold Python project).",
        )

    def test_no_scanner_bypass_flags(self):
        job = _job(self.workflow, "security")
        self.assertIsNotNone(
            job,
            "security job missing; cannot verify absence of bypass flags.",
        )
        step_texts = _job_step_texts(job)
        for step_text in step_texts:
            for label, pattern in _BYPASS_PATTERNS:
                with self.subTest(bypass=label, step=step_text[:80]):
                    self.assertIsNone(
                        pattern.search(step_text),
                        f"security job step uses forbidden bypass {label!r}; "
                        "scanners must fail closed.",
                    )

    def test_summary_needs_security(self):
        summary = _job(self.workflow, "summary")
        self.assertIsNotNone(
            summary,
            "Root workflow must retain the 'summary' gate job.",
        )
        needs = summary.get("needs")
        if isinstance(needs, str):
            needs_list = [needs]
        elif isinstance(needs, list):
            needs_list = list(needs)
        else:
            needs_list = []
        self.assertIn(
            "security",
            needs_list,
            "summary.needs must include 'security' so a security failure "
            "blocks the summary gate.",
        )

    def test_summary_fails_when_security_fails(self):
        summary = _job(self.workflow, "summary")
        self.assertIsNotNone(
            summary,
            "Root workflow must retain the 'summary' gate job.",
        )
        run_blobs = [
            step.get("run")
            for step in (summary.get("steps") or [])
            if isinstance(step, dict) and isinstance(step.get("run"), str)
        ]
        combined = "\n".join(run_blobs)
        self.assertRegex(
            combined,
            r"needs\.security\.result",
            "Root summary job's check-results step must inspect "
            "needs.security.result and exit nonzero when it is not 'success'.",
        )


class TemplateWorkflowJinjaSourceTests(unittest.TestCase):
    """Jinja-source assertions over ``ci.yml.jinja`` before rendering."""

    @classmethod
    def setUpClass(cls):
        cls.jinja_text = _text(TEMPLATE_WORKFLOW_JINJA)

    def test_every_github_expression_is_inside_raw_block(self):
        stripped = _RAW_BLOCK_RE.sub("", self.jinja_text)
        leftover = list(_GH_EXPR_RE.finditer(stripped))
        self.assertFalse(
            leftover,
            "Every '${{ ... }}' in ci.yml.jinja must be wrapped in "
            "'{% raw %}...{% endraw %}'. Unwrapped expressions get eaten by "
            "Copier and produce broken workflow YAML. Offending offsets in "
            f"the raw-block-stripped source: {[m.start() for m in leftover]}",
        )

    def test_rendered_workflow_preserves_github_token_secret(self):
        rendered = _render_template_workflow()
        self.assertIn(
            "${{ secrets.GITHUB_TOKEN }}",
            rendered,
            "The rendered template workflow must contain literal "
            "'${{ secrets.GITHUB_TOKEN }}' (Task 4 adds it to the security "
            "job's Gitleaks step, inside a raw block).",
        )


class TemplateRenderedWorkflowTests(unittest.TestCase):
    """Structural assertions over the rendered ``ci.yml``."""

    @classmethod
    def setUpClass(cls):
        cls.rendered = _render_template_workflow()
        try:
            cls.workflow = yaml.safe_load(cls.rendered)
        except yaml.YAMLError as exc:
            cls.workflow = None
            cls.yaml_error = exc
        else:
            cls.yaml_error = None

    def _require_workflow(self):
        if self.workflow is None:
            self.fail(
                "Rendered ci.yml.jinja is not valid YAML; cannot check "
                f"security-job structure. Error: {self.yaml_error}"
            )

    def test_rendered_workflow_yaml_parses(self):
        self._require_workflow()
        self.assertIsInstance(
            self.workflow.get("jobs"),
            dict,
            "Rendered workflow must have a 'jobs' mapping.",
        )

    def test_security_job_exists_in_rendered(self):
        self._require_workflow()
        self.assertIsNotNone(
            _job(self.workflow, "security"),
            "Rendered template ci.yml must define a top-level 'security' job.",
        )

    def test_security_job_checkout_uses_fetch_depth_zero(self):
        self._require_workflow()
        job = _job(self.workflow, "security")
        self.assertIsNotNone(
            job,
            "security job missing in rendered template ci.yml.",
        )
        checkouts = [
            step
            for step in (job.get("steps") or [])
            if isinstance(step, dict)
            and isinstance(step.get("uses"), str)
            and step["uses"].startswith("actions/checkout@")
        ]
        self.assertTrue(
            checkouts,
            "Template security job must include an actions/checkout step.",
        )
        depths = [(step.get("with") or {}).get("fetch-depth") for step in checkouts]
        self.assertIn(
            0,
            depths,
            "Template security-job checkout must set 'fetch-depth: 0' so "
            "Gitleaks can scan the local generated-project history.",
        )

    def test_gitleaks_pinned_sha_in_rendered(self):
        self._require_workflow()
        job = _job(self.workflow, "security")
        self.assertIsNotNone(job, "security job missing in rendered template ci.yml.")
        gitleaks_uses = [
            step.get("uses")
            for step in (job.get("steps") or [])
            if isinstance(step, dict)
            and isinstance(step.get("uses"), str)
            and step["uses"].lower().startswith("gitleaks/gitleaks-action@")
        ]
        self.assertTrue(
            gitleaks_uses,
            "Template security job must invoke gitleaks/gitleaks-action.",
        )
        for uses in gitleaks_uses:
            self.assertTrue(
                uses.endswith("@" + GITLEAKS_PINNED_SHA),
                f"Template gitleaks reference {uses!r} must be pinned to the "
                f"full commit SHA {GITLEAKS_PINNED_SHA!r}.",
            )

    def test_pip_audit_version_pin_in_rendered(self):
        self._require_workflow()
        job = _job(self.workflow, "security")
        self.assertIsNotNone(job, "security job missing in rendered template ci.yml.")
        blob = "\n".join(_job_step_texts(job))
        self.assertIn(
            PIP_AUDIT_PIN,
            blob,
            f"Template security job must install {PIP_AUDIT_PIN!r} verbatim.",
        )

    def test_generated_install_covers_dev_and_every_rendered_optional_extra(self):
        """The security job must install base+dev and every rendered optional extra."""
        self._require_workflow()
        job = _job(self.workflow, "security")
        self.assertIsNotNone(job, "security job missing in rendered template ci.yml.")
        blob = "\n".join(_job_step_texts(job))
        # requirements.txt.jinja carries base + dev; pyproject extras give
        # every optional set. With the render context above, pyproject
        # exposes: postgres, mcp, metrics, otel, load. All must be
        # installed before pip-audit runs so the resolved set matches what
        # a generated project actually uses.
        self.assertIn(
            "requirements.txt",
            blob,
            "Template security job must install base + development deps "
            "via requirements.txt before running pip-audit.",
        )
        for extra in ("postgres", "mcp", "metrics", "otel", "load"):
            with self.subTest(extra=extra):
                self.assertRegex(
                    blob,
                    rf"\.\[[^]]*\b{extra}\b[^]]*\]",
                    f"Template security job must install the {extra!r} extra "
                    "(rendered from pyproject.toml.jinja) so pip-audit sees "
                    "the same resolved set generated projects actually use.",
                )

    def test_audit_gate_invoked_locally(self):
        self._require_workflow()
        job = _job(self.workflow, "security")
        self.assertIsNotNone(job, "security job missing in rendered template ci.yml.")
        blob = "\n".join(_job_step_texts(job))
        self.assertIn(
            "scripts/pip_audit_gate.py",
            blob,
            "Template security job must invoke the audit gate shipped at "
            "scripts/pip_audit_gate.py inside the generated project.",
        )

    def test_no_scanner_bypass_flags_in_rendered(self):
        self._require_workflow()
        job = _job(self.workflow, "security")
        self.assertIsNotNone(job, "security job missing in rendered template ci.yml.")
        for step_text in _job_step_texts(job):
            for label, pattern in _BYPASS_PATTERNS:
                with self.subTest(bypass=label, step=step_text[:80]):
                    self.assertIsNone(
                        pattern.search(step_text),
                        f"Template security-job step uses forbidden bypass "
                        f"{label!r}; scanners must fail closed.",
                    )

    def test_summary_needs_test_lint_security(self):
        self._require_workflow()
        summary = _job(self.workflow, "summary")
        self.assertIsNotNone(
            summary,
            "Template rendered ci.yml must include a 'summary' gate job "
            "(Task 4 adds it).",
        )
        needs = summary.get("needs")
        if isinstance(needs, str):
            needs_list = [needs]
        elif isinstance(needs, list):
            needs_list = list(needs)
        else:
            needs_list = []
        for required in ("test", "lint", "security"):
            with self.subTest(required=required):
                self.assertIn(
                    required,
                    needs_list,
                    f"Template summary.needs must include {required!r} so a "
                    "failure in any of test/lint/security blocks the summary "
                    "gate.",
                )

    def test_summary_fails_when_any_dependency_fails(self):
        self._require_workflow()
        summary = _job(self.workflow, "summary")
        self.assertIsNotNone(
            summary,
            "Template summary job missing; cannot verify fail-closed logic.",
        )
        run_blobs = [
            step.get("run")
            for step in (summary.get("steps") or [])
            if isinstance(step, dict) and isinstance(step.get("run"), str)
        ]
        combined = "\n".join(run_blobs)
        for required in ("test", "lint", "security"):
            with self.subTest(required=required):
                self.assertRegex(
                    combined,
                    rf"needs\.{required}\.result",
                    f"Template summary must inspect needs.{required}.result "
                    "and exit nonzero unless it is 'success'.",
                )


class GeneratedBranchProtectionDocumentationTests(unittest.TestCase):
    """The template must name the summary job as the required branch-protection check."""

    def test_some_template_doc_names_summary_as_required_branch_protection_check(self):
        candidates: list[Path] = []
        for pattern in ("*.md", "*.md.jinja", "*.mdc", "*.mdc.jinja"):
            candidates.extend(TEMPLATE_ROOT.rglob(pattern))
        hits: list[Path] = []
        for path in candidates:
            try:
                lower = _text(path).lower()
            except (OSError, UnicodeDecodeError):
                continue
            if (
                "branch protection" in lower
                and "summary" in lower
                and "required" in lower
            ):
                hits.append(path)
        self.assertTrue(
            hits,
            "No file under template/{{project_slug}}/ names the CI summary "
            "job as the required branch-protection check. Task 4 must add "
            "generated setup docs stating that the summary job (which "
            "depends on test, lint, and security) is the required check.",
        )


class SecretScanningBaselineTests(unittest.TestCase):
    """Template must ship no known fake credential and no Gitleaks allowlist."""

    def test_no_fake_credential_literal_in_template(self):
        # Iterate the template tree once, checking each file for any of the
        # three known fixture literals. Any hit is a Task 3B regression.
        offenders: list[tuple[str, Path]] = []
        for path in TEMPLATE_ROOT.rglob("*"):
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for literal in FAKE_CREDENTIAL_LITERALS:
                if literal in content:
                    offenders.append((literal, path))
        self.assertEqual(
            offenders,
            [],
            "Fake-credential literals still present under template/. Task "
            "3B must replace them with in-memory fragment assembly so "
            "generated projects ship no Gitleaks findings. Offenders: "
            f"{[(lit[:6] + '...', str(p.relative_to(REPO_ROOT))) for lit, p in offenders]}",
        )

    def test_root_gitleaksignore_exists(self):
        self.assertTrue(
            ROOT_GITLEAKSIGNORE.exists(),
            "Root repository must ship a .gitleaksignore listing the "
            "historical fixture fingerprints so the blocking scanner in CI "
            "does not re-fire on immutable Git history.",
        )

    def test_root_gitleaksignore_entries_are_commit_scoped_fingerprints(self):
        if not ROOT_GITLEAKSIGNORE.exists():
            self.fail(
                f".gitleaksignore not present at {ROOT_GITLEAKSIGNORE}; "
                "cannot verify fingerprint format."
            )
        content = _text(ROOT_GITLEAKSIGNORE)
        content_lines: list[tuple[int, str]] = []
        for lineno, raw_line in enumerate(content.splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            content_lines.append((lineno, line))
        # A comment-only .gitleaksignore would pass every per-line regex
        # check vacuously; require at least one real fingerprint so the
        # test cannot be satisfied by an empty allowlist.
        self.assertGreater(
            len(content_lines),
            0,
            ".gitleaksignore contains no fingerprint entries. Task 3B must "
            "populate it with the historical fixture fingerprints; an empty "
            "allowlist next to a live blocking Gitleaks job means the first "
            "CI run will fail on immutable history.",
        )
        for lineno, line in content_lines:
            with self.subTest(line=lineno, value=line[:64]):
                self.assertRegex(
                    line,
                    _FINGERPRINT_RE,
                    f".gitleaksignore line {lineno} is not a commit-scoped "
                    "fingerprint '<40-hex-sha>:<path>:<rule>:<line>'. Globs, "
                    "wildcards, bare paths, and rule-wide entries are "
                    "forbidden -- they would mask unrelated future findings.",
                )

    def test_template_ships_no_gitleaks_allowlist_config(self):
        forbidden_names = (".gitleaks.toml", "gitleaks.toml", ".gitleaksignore")
        offenders: list[Path] = []
        for path in TEMPLATE_ROOT.rglob("*"):
            if path.is_file() and path.name in forbidden_names:
                offenders.append(path.relative_to(REPO_ROOT))
        self.assertEqual(
            offenders,
            [],
            "Generated projects must ship no Gitleaks allowlist or ignore "
            "config: fixtures are cleaned at the source, so first-run "
            "Gitleaks is clean without any shipped suppression. Offenders: "
            f"{offenders}",
        )


if __name__ == "__main__":
    unittest.main()
