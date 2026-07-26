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
TEMPLATE_PYPROJECT_JINJA = TEMPLATE_ROOT / "pyproject.toml.jinja"

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

# Second render context that flips the API-gateway toggle OFF. The ``load``
# extra in ``pyproject.toml.jinja`` is guarded by ``include_api_gateway``,
# so this alternate render is the only way to verify that the generated CI
# actually mirrors the conditional extras — a single all-on render would
# accept a security job that unconditionally installs ``.[load]`` regardless
# of the toggle. All other unconditional extras (``postgres``, ``mcp``,
# ``metrics``, ``otel``) must still install with the gateway off.
RENDER_CONTEXT_NO_API_GATEWAY = dict(RENDER_CONTEXT, include_api_gateway=False)
UNCONDITIONAL_EXTRAS = ("postgres", "mcp", "metrics", "otel")
API_GATEWAY_ONLY_EXTRAS = ("load",)

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


def _render_template_workflow(context: dict | None = None) -> str:
    """Render ``ci.yml.jinja`` with a copier answer set (default: all toggles on)."""
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    ctx = RENDER_CONTEXT if context is None else context
    return env.from_string(_text(TEMPLATE_WORKFLOW_JINJA)).render(**ctx)


def _step_run_texts(job: dict) -> list[str]:
    """Return every step's ``run:`` script text (never ``name``/``uses``/``with``).

    Used by tests that require an invocation to appear in what the step
    actually executes, not merely in a human-readable ``name:`` label.
    """
    out: list[str] = []
    for step in job.get("steps") or []:
        if isinstance(step, dict) and isinstance(step.get("run"), str):
            out.append(step["run"])
    return out


def _bypass_scan_texts(job: dict) -> list[str]:
    """Return the texts subject to the scanner-bypass scan.

    Only ``run:`` scripts and ``continue-on-error:`` values (both step-
    level and job-level) are inspected -- never ``name`` (a human label),
    ``uses`` (a version-pinned action reference), or ``with`` (typed
    action parameters). ``name``/``uses``/``with`` cannot bypass a
    scanner, and folding them into the scan blob would false-positive-
    fail on a step deliberately named e.g. "never continue-on-error"
    whose actual behavior is clean. Scoping to what actually executes
    plus the two YAML keys that suppress step/job failure removes that
    whole class of accidental failure.
    """
    texts: list[str] = []
    if "continue-on-error" in job:
        texts.append(f"continue-on-error: {job['continue-on-error']}")
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if isinstance(step.get("run"), str):
            texts.append(step["run"])
        if "continue-on-error" in step:
            texts.append(f"continue-on-error: {step['continue-on-error']}")
    return texts


def _job(workflow: dict, name: str) -> dict | None:
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    if not isinstance(jobs, dict):
        return None
    return jobs.get(name)


# Jinja ``{% if ... %}`` / ``{% endif %}`` tag matcher used to reason about
# which lines in a Jinja source live inside which conditional block. Only
# ``if`` and ``endif`` change the active-condition stack; ``elif``/``else``
# keep the same block open, so they are deliberately not captured here.
_JINJA_IF_ENDIF_RE = re.compile(r"\{%\s*-?\s*(if|endif)([^%]*?)-?\s*%\}")


def _active_conditions_at(text: str, position: int) -> list[str]:
    """Return the stack of active Jinja ``{% if ... %}`` conditions at ``position``.

    Iterates every ``{% if ... %}`` / ``{% endif %}`` tag before
    ``position`` (in source order) and returns the list of condition
    expressions currently in scope, innermost last. Malformed or
    unbalanced blocks return the stack in whatever state the walk
    produced; callers rely on the ``in`` check, which is tolerant of
    that.
    """
    stack: list[str] = []
    for match in _JINJA_IF_ENDIF_RE.finditer(text):
        if match.start() >= position:
            break
        kind = match.group(1)
        rest = match.group(2).strip()
        if kind == "if":
            stack.append(rest)
        elif kind == "endif" and stack:
            stack.pop()
    return stack


def _is_inside_api_gateway_guard(text: str, needle: str) -> bool:
    """Return True if the first occurrence of ``needle`` in ``text`` lies
    inside a ``{% if include_api_gateway %}...{% endif %}`` block.

    ``needle`` must appear at least once; callers assert that separately
    so an accidental rename of the target extra is caught rather than
    silently reported as "not guarded".
    """
    idx = text.find(needle)
    if idx == -1:
        return False
    return "include_api_gateway" in _active_conditions_at(text, idx)


def _branch_protection_windows(text: str) -> list[str]:
    """Return every 'branch protection' occurrence as a bounded local window.

    Each returned slice is anchored on a 'branch protection' match, bounded
    forward to the next markdown heading (or 500 chars) and backward by 200
    chars. This is the anchor-window pattern used by
    ``low_obligation_window`` / ``medium_concise_window`` in
    ``tests/test_development_process.py``; scoping the three-way co-occurrence
    to a single window here prevents concepts scattered across unrelated
    sections from vacuously satisfying the documentation contract.
    """
    lower = text.lower()
    windows: list[str] = []
    for match in re.finditer(r"branch protection", lower):
        start = max(0, match.start() - 200)
        after = lower[match.end() :]
        heading = re.search(r"\n#+[\s#]", after)
        if heading is not None:
            end = match.end() + heading.start()
        else:
            end = match.end() + 500
        windows.append(lower[start:end])
    return windows


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
        # The pin must appear in a step's ``run:`` script (what actually
        # executes), not merely in a step ``name:`` label. A step named
        # "Install pip-audit==2.10.1" whose ``run:`` is
        # ``pip install pip-audit`` (unpinned) would install a floating
        # version despite the human-readable label.
        blob = "\n".join(_step_run_texts(job))
        self.assertIn(
            PIP_AUDIT_PIN,
            blob,
            f"Root security job must install {PIP_AUDIT_PIN!r} verbatim in "
            "a step's `run:` script so the auditor version is pinned in CI. "
            "A step `name:` mention is not an install.",
        )

    def test_audit_gate_invoked_against_core(self):
        job = _job(self.workflow, "security")
        self.assertIsNotNone(
            job,
            "security job missing; cannot verify audit-gate invocation.",
        )
        # The invocation must be in a step's ``run:`` script, not merely
        # mentioned in a step's human-readable ``name:``. A ``name:``-only
        # match would let a step named "Audit core with pip_audit_gate.py"
        # satisfy the assertion without ever executing the gate.
        gate_runs = [
            run for run in _step_run_texts(job) if "pip_audit_gate.py" in run
        ]
        self.assertTrue(
            gate_runs,
            "Root security job must invoke the shared audit gate at "
            "template/{{project_slug}}/scripts/pip_audit_gate.py from a "
            "step's `run:` script (there is no second copy at the root, and "
            "a mention in `name:` is not an invocation).",
        )
        # ``core/`` (path-like) must appear inside the same step that runs
        # the gate, scoping the assertion to the invocation itself. A bare
        # ``\bcore\b`` word-boundary match would false-satisfy on unrelated
        # tokens like ``core-team``, ``core.txt``, or ``core-scan.txt`` (a
        # step auditing the template while writing to a file named after
        # ``core`` could technically satisfy the older pattern without ever
        # scoping the gate to the ``core/`` project directory). Require
        # path syntax: preceded by a delimiter (start-of-line, whitespace,
        # ``=``, ``/``, or a quote) and followed by ``/``.
        for run in gate_runs:
            self.assertRegex(
                run,
                r"(?:^|[\s=/'\"])core/",
                "Root security job's audit-gate `run:` must scope the "
                "invocation to the `core/` project directory using path "
                "syntax (e.g. ``pip_audit_gate.py --target core/``, "
                "``cd core``, or a config that references ``core/``). A "
                "bare word like ``core-team`` or ``core.txt`` does not "
                f"count. Offending step run:\n{run}",
            )

    def test_no_scanner_bypass_flags(self):
        job = _job(self.workflow, "security")
        self.assertIsNotNone(
            job,
            "security job missing; cannot verify absence of bypass flags.",
        )
        # A job-level ``continue-on-error: true`` would neuter the entire
        # security job regardless of step-level flags, so check that first
        # with a direct structural assertion (clearer failure message than
        # the pattern scan below, which also catches this case).
        self.assertNotEqual(
            job.get("continue-on-error"),
            True,
            "Root security job sets job-level `continue-on-error: true`, "
            "which turns every step's failure into a soft pass and defeats "
            "the entire fail-closed contract.",
        )
        # A workflow with zero steps cannot run any scanner and so cannot
        # fail closed; require at least one step before iterating.
        steps = job.get("steps") or []
        self.assertTrue(
            steps,
            "Root security job has no steps; a job with no steps cannot "
            "run any scanner and so cannot fail closed.",
        )
        # ``_bypass_scan_texts`` deliberately excludes ``name`` / ``uses`` /
        # ``with`` so a step named e.g. "never continue-on-error" cannot
        # false-positive-fail this scan. See helper docstring.
        for scan_text in _bypass_scan_texts(job):
            for label, pattern in _BYPASS_PATTERNS:
                with self.subTest(bypass=label, scan=scan_text[:80]):
                    self.assertIsNone(
                        pattern.search(scan_text),
                        f"security job uses forbidden bypass {label!r} in "
                        f"a `run:` script or `continue-on-error:` value; "
                        "scanners must fail closed. Offending scan text: "
                        f"{scan_text!r}.",
                    )

    def test_no_duplicate_root_audit_gate(self):
        # The canonical audit gate ships at
        # template/{{project_slug}}/scripts/pip_audit_gate.py; the root
        # workflow invokes the same file via a relative template path.
        # A second copy at repo-root scripts/ would silently drift out of
        # sync — one owner, one file.
        root_dup = REPO_ROOT / "scripts" / "pip_audit_gate.py"
        self.assertFalse(
            root_dup.exists(),
            f"Duplicate audit gate found at {root_dup}. The canonical "
            "location is template/{{project_slug}}/scripts/pip_audit_gate.py; "
            "the root workflow must invoke that same file, not a fork of it.",
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
        # The pin must live in a step's ``run:`` script; a step named
        # ``Install pip-audit==2.10.1`` whose ``run:`` is a bare
        # ``pip install pip-audit`` would install an unpinned version.
        blob = "\n".join(_step_run_texts(job))
        self.assertIn(
            PIP_AUDIT_PIN,
            blob,
            f"Template security job must install {PIP_AUDIT_PIN!r} verbatim "
            "in a step's `run:` script. A step `name:` mention is not an "
            "install.",
        )

    def test_generated_install_covers_dev_and_every_rendered_optional_extra(self):
        """The security job must install base+dev and every rendered optional extra."""
        self._require_workflow()
        job = _job(self.workflow, "security")
        self.assertIsNotNone(job, "security job missing in rendered template ci.yml.")
        # Scope to ``run:`` scripts: a step named ``Install extras
        # [postgres,mcp,metrics,otel,load]`` with an unrelated ``run:``
        # would satisfy a whole-step-blob check without installing
        # anything. The install actually happens in the shell script.
        blob = "\n".join(_step_run_texts(job))
        # requirements.txt.jinja carries base + dev; pyproject extras give
        # every optional set. With the render context above, pyproject
        # exposes: postgres, mcp, metrics, otel, load. All must be
        # installed before pip-audit runs so the resolved set matches what
        # a generated project actually uses.
        self.assertIn(
            "requirements.txt",
            blob,
            "Template security job must install base + development deps "
            "via requirements.txt (referenced from a step's `run:` script) "
            "before running pip-audit.",
        )
        # Bracket-form extras spec (``[postgres]``, ``[postgres,mcp,...]``)
        # is the only pip-supported way to install package extras from a
        # source tree -- but we do not pin the specific ``.[postgres]``
        # invocation form: quoted variants (``".[postgres]"``,
        # ``pkg[postgres]``, ``"$(pwd)[postgres]"``) are all valid ways to
        # request the same extra. The regex requires the ``[`` to be
        # preceded by a package-target-y character (word char, ``.``, or
        # ``)``) so a bare YAML flow list (``env: [postgres, mcp]``) or
        # an echoed string (``echo "[postgres,mcp]"``) does not
        # accidentally satisfy the check.
        for extra in (*UNCONDITIONAL_EXTRAS, *API_GATEWAY_ONLY_EXTRAS):
            with self.subTest(extra=extra):
                self.assertRegex(
                    blob,
                    rf"(?<=[\w.)])\[[^]]*\b{extra}\b[^]]*\]",
                    f"Template security job must install the {extra!r} extra "
                    "(rendered from pyproject.toml.jinja) so pip-audit sees "
                    "the same resolved set generated projects actually use. "
                    "The extras spec must be preceded by a package target "
                    "(e.g. `.[postgres]`, `pkg[postgres]`, or "
                    "`\"$(pwd)[postgres]\"`), not a bare flow list.",
                )

    def test_audit_gate_invoked_locally(self):
        self._require_workflow()
        job = _job(self.workflow, "security")
        self.assertIsNotNone(job, "security job missing in rendered template ci.yml.")
        # Require the invocation to appear in a step's ``run:`` (what the
        # step actually executes), not merely in a ``name:`` label.
        gate_runs = [
            run
            for run in _step_run_texts(job)
            if "scripts/pip_audit_gate.py" in run
        ]
        self.assertTrue(
            gate_runs,
            "Template security job must invoke the audit gate shipped at "
            "scripts/pip_audit_gate.py inside the generated project from a "
            "step's `run:` script; a bare mention in `name:` is not an "
            "invocation.",
        )

    def test_no_scanner_bypass_flags_in_rendered(self):
        self._require_workflow()
        job = _job(self.workflow, "security")
        self.assertIsNotNone(job, "security job missing in rendered template ci.yml.")
        # Job-level ``continue-on-error: true`` neuters the whole job; check
        # it before scanning steps (clearer failure message than the
        # pattern scan, which also catches this case).
        self.assertNotEqual(
            job.get("continue-on-error"),
            True,
            "Rendered template security job sets job-level "
            "`continue-on-error: true`, which turns every step's failure "
            "into a soft pass and defeats the fail-closed contract.",
        )
        steps = job.get("steps") or []
        self.assertTrue(
            steps,
            "Rendered template security job has no steps; a job with no "
            "steps cannot run any scanner and so cannot fail closed.",
        )
        for scan_text in _bypass_scan_texts(job):
            for label, pattern in _BYPASS_PATTERNS:
                with self.subTest(bypass=label, scan=scan_text[:80]):
                    self.assertIsNone(
                        pattern.search(scan_text),
                        f"Template security job uses forbidden bypass "
                        f"{label!r} in a `run:` script or "
                        "`continue-on-error:` value; scanners must fail "
                        f"closed. Offending scan text: {scan_text!r}.",
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


class TemplateRenderedWorkflowNoApiGatewayTests(unittest.TestCase):
    """Second render with ``include_api_gateway=False``: conditional extras.

    The contract requires the security job to install exactly the extras
    that ``pyproject.toml.jinja`` rendered — no more, no less. The default
    render context has every optional toggle ON, so it cannot distinguish
    between a workflow that installs ``.[load]`` unconditionally and one
    that only installs it when the API gateway is present. Rendering a
    second time with the gateway OFF exercises the conditional so the
    ``load`` extra (guarded by ``include_api_gateway`` in the pyproject
    template) must be absent while the unconditional extras remain.
    """

    @classmethod
    def setUpClass(cls):
        cls.rendered = _render_template_workflow(RENDER_CONTEXT_NO_API_GATEWAY)
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
                "Rendered ci.yml.jinja (API gateway OFF) is not valid YAML; "
                "cannot check conditional-extras contract. Error: "
                f"{self.yaml_error}"
            )

    def test_rendered_workflow_yaml_parses_without_api_gateway(self):
        self._require_workflow()
        self.assertIsInstance(
            self.workflow.get("jobs"),
            dict,
            "Rendered workflow (API gateway OFF) must have a 'jobs' mapping.",
        )

    def test_load_extra_absent_when_api_gateway_off(self):
        self._require_workflow()
        job = _job(self.workflow, "security")
        self.assertIsNotNone(
            job,
            "security job missing in API-gateway-off render; cannot verify "
            "conditional-extras behavior.",
        )
        # Scope to ``run:`` scripts and use the anchored extras pattern
        # (``[`` preceded by a word char, ``.``, or ``)``) so a bare
        # YAML flow list or an echoed string cannot false-fail this
        # negative assertion. See the positive assertion in
        # ``test_generated_install_covers_dev_and_every_rendered_optional_extra``
        # for the pattern rationale.
        blob = "\n".join(_step_run_texts(job))
        for extra in API_GATEWAY_ONLY_EXTRAS:
            with self.subTest(extra=extra):
                self.assertNotRegex(
                    blob,
                    rf"(?<=[\w.)])\[[^]]*\b{extra}\b[^]]*\]",
                    f"Template security job installs the {extra!r} extra "
                    "even though include_api_gateway=False. The load extra "
                    "is only defined in pyproject.toml.jinja when the API "
                    "gateway is present; installing it unconditionally "
                    "would break `pip install` in generated projects that "
                    "opt out of the gateway.",
                )

    def test_unconditional_extras_still_present_when_api_gateway_off(self):
        self._require_workflow()
        job = _job(self.workflow, "security")
        self.assertIsNotNone(
            job,
            "security job missing in API-gateway-off render; cannot verify "
            "that unconditional extras still install.",
        )
        blob = "\n".join(_step_run_texts(job))
        for extra in UNCONDITIONAL_EXTRAS:
            with self.subTest(extra=extra):
                self.assertRegex(
                    blob,
                    rf"(?<=[\w.)])\[[^]]*\b{extra}\b[^]]*\]",
                    f"Template security job (API gateway OFF) must still "
                    f"install the {extra!r} extra: it is unconditional in "
                    "pyproject.toml.jinja. The extras spec must be preceded "
                    "by a package target (e.g. `.[postgres]`, "
                    "`pkg[postgres]`), not a bare flow list.",
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
                content = _text(path)
            except (OSError, UnicodeDecodeError):
                continue
            # The three concepts must co-occur inside a single bounded
            # window (paragraph or section) — not scattered anywhere in
            # the whole file. This follows the anchor-window pattern used
            # by ``low_obligation_window`` / ``medium_concise_window`` in
            # ``tests/test_development_process.py``. A file-wide substring
            # check would let a stray "branch protection" in one section,
            # "summary" in an unrelated table of contents, and "required"
            # in a third section all quilt together into a false pass.
            for window in _branch_protection_windows(content):
                if "summary" in window and "required" in window:
                    hits.append(path)
                    break
        self.assertTrue(
            hits,
            "No file under template/{{project_slug}}/ names the CI summary "
            "job as the required branch-protection check within a single "
            "documentation window (bounded by the next markdown heading or "
            "500 chars around the 'branch protection' anchor). Task 4 must "
            "add generated setup docs stating -- in one paragraph or "
            "section -- that the summary job (which depends on test, lint, "
            "and security) is the required check.",
        )


class PyprojectExtrasGuardTests(unittest.TestCase):
    """Guard that ``pyproject.toml.jinja``'s extras layout matches the
    assumptions baked into ``UNCONDITIONAL_EXTRAS`` and
    ``API_GATEWAY_ONLY_EXTRAS`` above.

    The rendered-workflow tests hard-code those two tuples: the security
    job must install every unconditional extra in both render contexts,
    and must install ``load`` only when ``include_api_gateway=True``. If
    a future edit relocates ``load`` outside the
    ``{% if include_api_gateway %}`` guard, or wraps an unconditional
    extra inside it, both tuples become silently wrong and the CI
    contract loses meaning. This class asserts the source-level shape
    directly so the drift is caught at test time rather than at install
    time in a generated project.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = _text(TEMPLATE_PYPROJECT_JINJA)

    def test_load_extra_is_guarded_by_api_gateway(self):
        needle = "load = ["
        self.assertIn(
            needle,
            self.text,
            f"pyproject.toml.jinja no longer defines a {needle!r} extra. "
            "API_GATEWAY_ONLY_EXTRAS lists 'load' and the API-gateway-off "
            "workflow tests hard-code that assumption; update both if the "
            "extra was intentionally removed.",
        )
        self.assertTrue(
            _is_inside_api_gateway_guard(self.text, needle),
            "pyproject.toml.jinja: 'load = [...]' must live inside a "
            "'{% if include_api_gateway %}' block so the extra is only "
            "defined when the generated project ships the API gateway. "
            "API_GATEWAY_ONLY_EXTRAS relies on this shape; moving 'load' "
            "outside the guard would silently invalidate the "
            "TemplateRenderedWorkflowNoApiGatewayTests contract without "
            "changing any workflow test.",
        )

    def test_unconditional_extras_are_not_guarded_by_api_gateway(self):
        for extra in UNCONDITIONAL_EXTRAS:
            needle = f"{extra} = ["
            with self.subTest(extra=extra):
                self.assertIn(
                    needle,
                    self.text,
                    f"pyproject.toml.jinja no longer defines a "
                    f"{needle!r} extra. UNCONDITIONAL_EXTRAS lists "
                    f"{extra!r}; update the tuple and the workflow tests "
                    "together if this was intentional.",
                )
                self.assertFalse(
                    _is_inside_api_gateway_guard(self.text, needle),
                    f"pyproject.toml.jinja: {needle!r} is inside a "
                    "'{% if include_api_gateway %}' block, but "
                    f"UNCONDITIONAL_EXTRAS lists {extra!r} as "
                    "always-defined. Either move the extra out of the "
                    "guard or move it from UNCONDITIONAL_EXTRAS to "
                    "API_GATEWAY_ONLY_EXTRAS -- silently splitting the "
                    "two invariants across files loses the CI contract.",
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
