# AI-Native SDLC Assurance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt risk-tiered design gates, closed-loop bug-class guidance,
blocking secret/dependency scans, evidence-backed reviews, and reviewer
assurance/shadow mode in both roundtable and generated projects.

**Architecture:** Governance remains Markdown and agent-instruction driven;
deterministic enforcement remains in CI and Python scripts. Root policy is the
source of truth, template assets mirror downstream obligations, and test
fixtures verify that policy, workflows, and deterministic findings remain
wired. Prompt-based reviewer evaluation stays human-gated because CI has no
authenticated agent runner.

**Tech Stack:** Python 3.13, `unittest`, GitHub Actions, Gitleaks, `pip-audit`,
Bandit, Copier/Jinja, Cursor rules and agent definitions.

## Global Constraints

- Implement as four sequential, focused PRs on branches; never commit to
  `main`.
- High-tier changes require architecture map, data flow, workflow states, and
  threat model.
- Secret and known-vulnerability findings block immediately.
- Pin `gitleaks/gitleaks-action` to commit
  `ff98106e4c7b2bc287b24eaf42907196329070c7` (release `v2.3.9`, SHA verified
  against the upstream tag on 2026-07-26) and install `pip-audit==2.10.1`.
  The action is license-free for public repositories and personal accounts;
  generated projects in a GitHub organization must supply `GITLEAKS_LICENSE`,
  which the scaffold documents but cannot provide.
- Highest applicable risk tier wins; changed-line count cannot downgrade a
  sensitive path or invariant.
- Only verified findings may block; `UNVERIFIED` concerns remain visible but
  non-blocking.
- Agents never auto-update their rules, promote reviewers, merge, push, or
  deploy.
- Template files containing Jinja placeholders retain a `.jinja` suffix.
- Run `bash scripts/validate_generated.sh` and inspect warnings before review.
- Each PR receives Bugbot plus its matching domain expert; findings require a
  regression test and re-review.
- Do not create commits or PRs until the maintainer explicitly requests them.

---

## PR 1: Risk-Tiered Design Gates

### Task 1: Add policy consistency tests first

**Files:**
- Create: `tests/test_development_process.py`
- Modify: `.github/workflows/validate.yml`

**Interfaces:**
- Consumes: root and template process/rule Markdown.
- Produces: a documentation-parity check for High/Medium/Low tiers and four
  high-tier artifacts. Human/domain review remains the actual classification
  gate.

- [ ] Write `unittest` cases that read the root and generated process assets and
  assert:
  - all three tiers exist;
  - “highest applicable tier wins” is present;
  - High requires architecture map, data flow, workflow states/wireframes, and
    threat model;
  - Medium is the default for every non-High/non-Low change and requires one
    concise design note;
  - Low exempts design artifacts only;
  - the under-20-line exemption means gross additions plus deletions, excludes
    generated artifacts from the count, and excludes high-tier paths and
    invariants;
  - both process docs and both root/template always-applied development-process
    rules carry the policy.
- [ ] Run `python -m unittest discover -s tests -p 'test_development_process.py' -v`.
  Expected: failure because the tier policy and threat-model requirement do not
  yet exist.
- [ ] Add `python -m unittest discover -s tests -v` to the root workflow’s
  `quick-checks` job, and install `pyyaml` alongside `jinja2` so the existing
  Copier security test also runs.

### Task 2: Implement the tier policy and fourth artifact

**Files:**
- Modify: `docs/DEVELOPMENT_PROCESS.md`
- Modify: `.cursor/rules/development-process.mdc`
- Modify: `template/{{project_slug}}/docs/DEVELOPMENT_PROCESS.md`
- Create: `template/{{project_slug}}/.cursor/rules/development-process.mdc`
- Modify: `template/{{project_slug}}/.cursor/agents/design-doc-author.md`
- Modify: `template/{{project_slug}}/docs/INDEX.md.jinja`
- Modify: `scripts/validate_generated.sh`

**Interfaces:**
- Consumes: the approved policy in the parent specification.
- Produces: deterministic tier-selection guidance and
  `docs/designs/<feature>/THREAT_MODEL.md` authoring guidance.

- [ ] Replace the blanket “every meaningful feature gets three docs” rule with
  explicit High/Medium/Low requirements and the highest-tier-wins override.
- [ ] Define High triggers for security/auth, identity/permissions,
  enforcement, migrations/schema/RLS, tenant/learning data, secrets/deployment
  boundaries, CI, and hooks. Include applicable root and generated path globs,
  including `src/*/security/**`, auth middleware, identity/scope dispatch,
  `enforcement/**`, `learning/tables.py` and migrations, `.github/workflows/**`,
  `.cursor/hooks*`, deployment manifests, and secret configuration.
- [ ] Define Medium as the default for any non-High/non-Low change, including
  single-file behavior changes, with one concise note covering architecture,
  data flow, failure behavior, risks, and tests.
- [ ] Define Low as docs/presentation-only, test-only, or under-20-line fixes
  measured as gross additions plus deletions (excluding mechanically generated
  artifacts) that do not touch High paths/invariants. Record the tier/rationale
  in the PR description. Preserve branch, CI, human ownership, and applicable
  review.
- [ ] Update the workflow diagram and gate descriptions to show tier
  classification and the threat-model branch.
- [ ] Extend `design-doc-author.md` with the threat-model template: assets,
  actors, trust boundaries, abuse cases, controls, residual risks, and security
  acceptance criteria. Update “three” references to “four for High,” and use
  `docs/designs/<feature>/` as the primary generated-project layout.
- [ ] Update the generated documentation index to include threat-model
  artifacts, and update the Phased Model section so it no longer assumes the
  old three-document policy.
- [ ] Fix generated validation so the root `.cursor` directory alone is
  no longer ignored by name at every depth; retain nested
  `template/{{project_slug}}/.cursor`. Remove the fallback renderer’s blanket
  `.cursor` skip. The Copier `_subdirectory` still prevents root-only rules
  from entering generated output.
- [ ] Add per-profile assertions for the rendered development-process rule,
  design-doc author, tier policy, threat-model guidance, and INDEX links.
- [ ] Run the focused unit test again. Expected: pass.
- [ ] Run `python scripts/quick_checks.py`. Expected: pass with no new warning.
- [ ] Run `bash scripts/validate_generated.sh`. Expected: all profiles pass;
  inspect and record every warning.
- [ ] Request post-diff Bugbot and template-DX review. Fix confirmed findings
  with regression tests and repeat both reviews until `APPROVED`.

---

## PR 2: Blocking Secret and Dependency Scans

### Task 3: Pin the workflow contract with failing tests

**Files:**
- Create: `tests/test_ci_security.py`
- Create: `tests/test_pip_audit_gate.py`

**Interfaces:**
- Consumes: root and generated GitHub Actions source.
- Produces: assertions that scanners are present, blocking, correctly scoped,
  and included in root summary gating.

- [ ] Write `unittest` cases asserting that:
  - root and template workflows contain a dedicated `security` job;
  - checkout sets literal `fetch-depth: 0` for Gitleaks;
  - Gitleaks uses pinned commit
    `ff98106e4c7b2bc287b24eaf42907196329070c7`;
  - `pip-audit==2.10.1` is installed;
  - root invokes the audit gate for `core/`;
  - generated CI installs base/development and every rendered optional extra,
    then invokes the audit gate against the local environment;
  - each new GitHub expression in the Jinja workflow is inside a raw block;
  - a rendered workflow preserves `${{ secrets.GITHUB_TOKEN }}`;
  - no scanner command uses `continue-on-error`, `|| true`, or an auto-fix flag;
  - root `summary.needs` includes `security` and fails when it is unsuccessful.
  - generated `summary.needs` includes `test`, `lint`, and `security`, fails
    unless all succeed, and is the documented required branch-protection check.
- [ ] Write audit-gate unit tests for valid active exceptions, missing fields,
  duplicate IDs, malformed dates, expired dates, and propagation of nonzero
  `pip-audit` exit codes. Assert an expired entry never reaches the auditor.
- [ ] Write secret-scanning-baseline tests asserting that:
  - the three known fake-credential literals
    (`correct-key-12345`, `0123456789abcdef`, and the `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.`
    JWT prefix) appear in no file under `template/`;
  - the root `.gitleaksignore` exists and every non-comment, non-blank line is a
    commit-scoped fingerprint (`<40-hex-sha>:<path>:<rule>:<line>`) — no glob,
    wildcard, bare path, or rule-wide entry;
  - no Gitleaks allowlist config (`.gitleaks.toml`, `gitleaks.toml`,
    `.gitleaksignore`) exists anywhere under `template/`, so generated projects
    ship no suppression.
- [ ] Run `python -m unittest discover -s tests -p 'test_ci_security.py' -v`.
  Expected: failure because neither workflow has the new security job and the
  baseline is not yet clean.

### Task 3B: Clean the secret-scanning baseline

**Files:**
- Modify: `template/{{project_slug}}/tests/test_middleware.py.jinja`
- Modify: `template/{{project_slug}}/tests/test_agent_identity.py.jinja`
- Modify: `template/{{project_slug}}/evals/fixtures/injection_defense_dataset.json`
- Modify: the eval loader that consumes the injection dataset
- Create: `.gitleaksignore`

**Interfaces:**
- Consumes: the 2026-07-26 preflight findings.
- Produces: template fixtures that no longer match Gitleaks, and a
  commit-scoped historical baseline for the root repository only.

The preflight established that `pip-audit==2.10.1` against `core/` is clean and
that Gitleaks 8.30.1 reports seven history findings, all fake test values in
three template fixtures. No real secret exists, so PR 2 continues; these steps
clean the baseline at the source rather than suppressing it by policy.

- [ ] Rewrite the three fixtures so no committed line matches Gitleaks, while
  preserving each fixture's original test semantics:
  - `test_middleware.py.jinja` — the `API_KEY` / credentials / assertion trio
    currently sharing the literal `correct-key-12345`;
  - `test_agent_identity.py.jinja` — `TEST_SIGNING_KEY`, which must still
    produce a 64-character key meeting the documented minimum;
  - `injection_defense_dataset.json` — the `benign-enc-jwt` case, whose input
    must still be a structurally valid JWT that decodes to benign claims so the
    eval's `expected: pass` disposition is unchanged.
- [ ] Assemble secret-like values in memory (fragment join, derivation, or
  generation) at the point of use. Do not add a Gitleaks allowlist, ignore
  file, or inline `gitleaks:allow` comment anywhere under `template/`.
- [ ] Where the eval dataset needs assembly, extend the loader with an explicit
  fragment-join field rather than special-casing one record. Keep the loader's
  behavior unchanged for records that do not use it.
- [ ] Create the root `.gitleaksignore` with exactly the seven historical
  fingerprints below, each preceded by a comment naming the fixture and why the
  value is not a secret. Do not add any other entry.
  - `76b141744c8b1cde809d26104d76cca389217f26:template/{{project_slug}}/evals/fixtures/injection_defense_dataset.json:jwt:63`
  - `71db771c2ac3f120c0f25b8e63417ba325d1a1ad:template/{{project_slug}}/evals/fixtures/injection_defense_dataset.json:jwt:63`
  - `211a8f723f8ac890a039603da7db89eb630d1369:template/{{project_slug}}/tests/test_agent_identity.py.jinja:generic-api-key:33`
  - `cf51b293f04d368549d87f37d9f9160f6767963e:template/{{project_slug}}/tests/test_agent_identity.py.jinja:generic-api-key:33`
  - `98a1baaa2463722ef422a350dad666ac3e651607:template/{{project_slug}}/tests/test_middleware.py.jinja:generic-api-key:115`
  - `98a1baaa2463722ef422a350dad666ac3e651607:template/{{project_slug}}/tests/test_middleware.py.jinja:generic-api-key:118`
  - `98a1baaa2463722ef422a350dad666ac3e651607:template/{{project_slug}}/tests/test_middleware.py.jinja:generic-api-key:121`
- [ ] Run the baseline tests from Task 3. Expected: pass.
- [ ] Run `gitleaks detect --source . --no-banner --exit-code 1` (git history)
  and `gitleaks detect --source . --no-git --exit-code 1` restricted to tracked
  content. Expected: zero findings with the new ignore file in place.
- [ ] Run the affected generated tests (middleware, agent identity) and the
  injection-defense eval against a rendered project. Expected: unchanged
  pass/flag dispositions and unchanged eval scores.

### Task 4: Add blocking scanners

**Files:**
- Modify: `.github/workflows/validate.yml`
- Modify: `template/{{project_slug}}/.github/workflows/ci.yml.jinja`
- Create: `template/{{project_slug}}/scripts/pip_audit_gate.py`
- Create: `.github/pip-audit-exceptions.json`
- Create: `template/{{project_slug}}/.github/pip-audit-exceptions.json`
- Modify: `docs/DEVELOPMENT_PROCESS.md`
- Modify: `template/{{project_slug}}/docs/DEVELOPMENT_PROCESS.md`
- Modify: `template/{{project_slug}}/docs/GOVERNANCE.md`
- Modify: `template/{{project_slug}}/docs/OPERATIONS.md.jinja`
- Modify: `scripts/validate_generated.sh`

**Interfaces:**
- Consumes: repository history and Python dependency declarations.
- Produces: independent blocking Gitleaks and `pip-audit` signals.

- [ ] Confirm the Task 3B baseline is still clean immediately before the
  workflow edits: pinned Gitleaks over history and working tree, and
  `pip-audit==2.10.1` against `core/`. If either now finds a real issue, stop
  and remediate it rather than weakening the new gate.
- [ ] Add a root `security` job that checks out with `fetch-depth: 0`, runs the
  commit-pinned Gitleaks action, installs pinned `pip-audit`, and invokes the
  canonical audit gate at
  `template/{{project_slug}}/scripts/pip_audit_gate.py` for `core/`.
- [ ] Add `security` to the root summary’s `needs` list and explicit success
  condition.
- [ ] Move generated Bandit execution from `lint` into a dedicated `security`
  job, then add full-history pinned Gitleaks. Wrap every new GitHub expression
  in `{% raw %}` / `{% endraw %}`. Keep lint and format checks independent.
- [ ] In generated security CI, install `requirements.txt`, install the project
  with all rendered extras (`postgres,mcp,metrics,otel` and `load` when the API
  gateway exists), then audit the local environment through the gate. This
  covers base, development, and optional dependencies actually resolved by CI.
- [ ] Add a generated `summary` job that runs with `if: always()`, needs
  `test`, `lint`, and `security`, and exits nonzero unless every result is
  `success`.
- [ ] Implement the fail-closed audit gate. It validates the JSON exception
  schema (`id`, `reason`, `owner`, `compensating_control`, `expires`), rejects
  duplicates/malformed/expired entries, converts active IDs to explicit
  `--ignore-vuln` arguments, and returns the auditor’s exact exit code.
- [ ] Start both exception files with an empty list. Do not use
  `pip-audit --fix`, `continue-on-error`, `|| true`, a blanket ignore, or
  warning-only behavior.
- [ ] Document range/non-lockfile limits, advisory-database limits, external
  branch-protection limits, and manual prompt-review limits in generated
  GOVERNANCE Non-Claims.
- [ ] Add generated operations guidance naming `summary` as the single check
  users must require in branch protection and explaining that it includes
  `security`. State that the scaffold cannot configure or verify this external
  setting.
- [ ] Add a generated-validation dependency-audit step for each rendered
  requirements set; on the full profile, install/audit all optional extras.
  Fail on auditor errors or findings.
- [ ] Add per-profile assertions for the rendered `security` job,
  `fetch-depth: 0`, pinned versions, intact
  `${{ secrets.GITHUB_TOKEN }}`, audit gate, exception file, branch
  protection guidance, and the absence of any Gitleaks allowlist or ignore
  file in the generated project.
- [ ] Run the focused workflow test. Expected: pass.
- [ ] Render the template and inspect the generated workflow for intact GitHub
  expressions and no raw Jinja leakage.
- [ ] Run `bash scripts/validate_generated.sh`. Expected: all profiles pass.
- [ ] Request Bugbot plus SRE/security-architect review. Re-test and re-review
  until both return `APPROVED`.
- [ ] Before merge, have the maintainer configure GitHub branch protection to
  require the new root `security` job; record that this external setting is not
  provable from repository files.
- [ ] Treat scanner network/database unavailability as a failed job. A human may
  rerun a transient failure, but may not convert it to warning-only behavior.

---

## PR 3: Evidence-Backed Findings and Bug-Class Feedback

### Task 5: Add failing governance and deterministic-output tests

**Files:**
- Create: `tests/test_review_governance.py`
- Create: `tests/test_agent_review.py`

**Interfaces:**
- Consumes: reviewer definitions, process docs, bug registers, and
  `scripts/agent_review.py`.
- Produces: a shared blocking-evidence contract and stable deterministic rule
  identifiers.

- [ ] Write governance tests that assert the root/template process requires
  one-off-versus-class classification and that recurring classes require a
  regression test, relevant instruction update, and register entry.
- [ ] Assert the expert-review rule defines `UNVERIFIED` as non-blocking and
  requires location, execution/exploit path, trigger/reproduction, defense
  challenge, impact, and remediation for blocking findings.
- [ ] Assert scoped reviewer definitions reference the same contract and never
  grant merge, fix, or self-promotion authority. Assert always-applied reviewer
  rules consult the assurance register and only a recorded `BLOCKING` reviewer
  may recommend a block.
- [ ] Write deterministic-review tests that call scanner functions with virtual
  source paths and assert failures include stable IDs such as
  `SEC-SQL-FSTRING`, `SEC-SHELL-TRUE`, and `SEC-HARDCODED-CREDENTIAL`, exact
  location, and matched evidence.
- [ ] Run both focused test files. Expected: failure because the contract,
  registers, and stable IDs do not exist.

### Task 6: Implement the evidence contract

**Files:**
- Modify: `template/{{project_slug}}/.cursor/rules/expert-review.mdc`
- Modify: `template/{{project_slug}}/.cursor/rules/red-team.mdc`
- Modify: `template/{{project_slug}}/.cursor/agents/red-team.md`
- Modify: `template/{{project_slug}}/.cursor/agents/sast-reviewer.md`
- Modify: `template/{{project_slug}}/.cursor/agents/security-hardener.md`
- Modify: `template/{{project_slug}}/.cursor/agents/agent-security-specialist.md`
- Modify: `template/{{project_slug}}/.cursor/agents/code-reviewer.md`
- Modify: `template/{{project_slug}}/.cursor/agents/solution-architect.md`
- Modify: `template/{{project_slug}}/.cursor/agents/test-architect.md`
- Modify: `template/{{project_slug}}/.cursor/agents/data-flow-guardian.md`
- Modify: `scripts/agent_review.py`
- Modify: `template/{{project_slug}}/scripts/red_team_check.py`

**Interfaces:**
- Produces: verified blocking findings, non-blocking `UNVERIFIED` concerns, and
  stable machine-testable deterministic messages.

- [ ] Add the common evidence fields to `expert-review.mdc`, with
  domain-specific requirements for security and correctness findings.
- [ ] Require every potential blocker to challenge existing defenses before
  reaching a confirmed verdict.
- [ ] Add `UNVERIFIED` handling to the red-team rule and each scoped reviewer;
  keep concerns visible but exclude them from blocking and clean-slate counts.
- [ ] Resolve always-applied red-team semantics explicitly: the rule always
  performs analysis, but its verdict is blocking only when that exact reviewer
  version is `BLOCKING` in the assurance register.
- [ ] Preserve the SAST reviewer’s stronger four-gate/dual-pass protocol and
  map it to the shared fields instead of weakening or duplicating it.
- [ ] Refactor deterministic findings to carry stable rule ID, severity,
  location, message, and matched evidence. Keep warning/failure exit behavior
  unchanged in both root `agent_review.py` and generated `red_team_check.py`.
- [ ] Run `tests/test_agent_review.py` and the existing
  `scripts/agent_review.py` validation path. Expected: pass with unchanged
  detection outcomes.

### Task 7: Implement bug-class feedback

**Files:**
- Create: `docs/BUG_CLASS_REGISTER.md`
- Create: `template/{{project_slug}}/docs/BUG_CLASS_REGISTER.md`
- Modify: `docs/DEVELOPMENT_PROCESS.md`
- Modify: `.cursor/rules/development-process.mdc`
- Modify: `template/{{project_slug}}/docs/DEVELOPMENT_PROCESS.md`
- Modify: `template/{{project_slug}}/.cursor/rules/expert-review.mdc`
- Modify: `template/{{project_slug}}/docs/INDEX.md.jinja`

**Interfaces:**
- Produces: a human-gated recurrence decision and durable links from finding to
  invariant, instruction, and regression test.

- [ ] Add register schemas with fields for ID, date/source, classification,
  invariant, affected scope, relevant rule, regression test, owner, and status.
  Begin with no invented historical bug classes.
- [ ] Add the completion gate: a recurring class cannot be approved without all
  three linked artifacts.
- [ ] Require generated-project rules to update the nearest relevant
  instruction, not merely append prose to the register.
- [ ] State that agents may propose but cannot auto-classify, self-edit
  instructions, or approve their own rule changes.
- [ ] Add per-profile generated-validation assertions for both bug-class
  registers, the rendered completion gate, and documentation-index links.
- [ ] Run both focused test files. Expected: pass.
- [ ] Run `bash scripts/validate_generated.sh`. Expected: all profiles pass.
- [ ] Request Bugbot plus security-architect and template-DX reviews. Re-test
  and re-review until all return `APPROVED`.

---

## PR 4: Reviewer Red-Team Fixtures and Shadow Mode

### Task 8: Write seeded cases and failing harness tests

**Files:**
- Create: `template/{{project_slug}}/reviewer-evals/cases.json`
- Create: `template/{{project_slug}}/reviewer-evals/README.md`
- Create: `template/{{project_slug}}/scripts/reviewer_eval.py`
- Create: `tests/test_reviewer_evals.py`
- Modify: `template/{{project_slug}}/.github/workflows/ci.yml.jinja`
- Modify: `scripts/validate_generated.sh`

**Interfaces:**
- `cases.json` entries contain `id`, `domain`, `execution_mode`,
  `virtual_path`, `content_fragments`, `expected_disposition`,
  `expected_rule_ids`, and `manual_reviewers`.
- Deterministic cases feed scanner functions without writing vulnerable files
  into production paths.
- Manual cases are copied as untrusted review input to prompt reviewers.

- [ ] Seed vulnerable and safe-near-miss pairs for hardcoded secrets, SQL
  injection, unsafe shell execution, path traversal, missing auth, missing
  tenant scope, and prompt-injection boundary handling.
- [ ] Publish an explicit coverage matrix: secrets/SQL/unsafe shell run
  deterministically and manually; path traversal/auth/tenant/prompt-boundary
  cases are `MANUAL_AGENT` unless a tested deterministic rule exists. CI and
  docs must never claim manual-only domains are deterministically proven.
- [ ] Use unmistakably fake credentials and non-executable snippets. Store
  secret-like examples as low-entropy fragments that do not match Gitleaks in
  repository text; assemble them only in memory inside the harness.
- [ ] Write schema tests requiring a unique ID, both vulnerable and safe cases
  per domain, explicit execution mode/disposition, and prompt-injection text
  treated as data. Assert no committed fixture line contains the assembled
  secret marker.
- [ ] Parametrize deterministic cases through `scripts/agent_review.py` and
  assert exact expected rule IDs without weakening the test-file exclusion used
  during ordinary repository scans.
- [ ] Implement the shipped deterministic runner using generated
  `scripts/red_team_check.py`; it validates fixture schema, runs only
  `DETERMINISTIC` cases, and returns nonzero for missed vulnerabilities, false
  blocking safe cases, or schema errors.
- [ ] Run the shipped deterministic runner in generated `security` CI and in
  `validate_generated.sh`. Add per-profile assertions that the runner, cases,
  README, coverage modes, and CI command render.
- [ ] Run `python -m unittest discover -s tests -p 'test_reviewer_evals.py' -v`.
  Expected: fail until the fixture schema and any missing deterministic checks
  are complete.

### Task 9: Add shadow and promotion governance

**Files:**
- Create: `docs/REVIEWER_ASSURANCE.md`
- Create: `template/{{project_slug}}/docs/REVIEWER_ASSURANCE.md`
- Modify: `docs/DEVELOPMENT_PROCESS.md`
- Modify: `.cursor/rules/development-process.mdc`
- Modify: `template/{{project_slug}}/docs/DEVELOPMENT_PROCESS.md`
- Modify: `template/{{project_slug}}/.cursor/rules/expert-review.mdc`
- Modify: `template/{{project_slug}}/docs/INDEX.md.jinja`
- Modify: `template/{{project_slug}}/docs/GOVERNANCE.md`
- Modify: `scripts/validate_generated.sh`

**Interfaces:**
- Reviewer states: `DRAFT`, `SHADOW`, `BLOCKING`, `SUSPENDED`.
- Promotion record: reviewer, version/change reference, fixture-set version,
  detection result, safe-case result, injection-resistance result, evidence
  review, human approver, date.

- [ ] Document that new or materially changed prompt reviewers begin in
  `SHADOW`, may comment, and cannot block.
- [ ] Require all vulnerable cases detected, zero false blocking safe cases,
  complete evidence, injection resistance, and recorded human approval before
  promotion.
- [ ] Define material changes (prompt, scope, tools, model behavior) and return
  them to shadow; exempt behavior-neutral editorial changes.
- [ ] Define suspension on missed seeded cases, false blocking results,
  instruction following from untrusted fixtures, or scope overreach.
- [ ] State plainly that deterministic cases run in CI while prompt-reviewer
  runs are manual because no authenticated agent runner exists.
- [ ] Document the downstream promotion procedure: run the shipped deterministic
  command, feed only the reviewer’s declared `MANUAL_AGENT` cases in fresh
  contexts, record case IDs/evidence, and obtain human approval.
- [ ] Add an initial assurance row for every existing prompt reviewer with
  status `SHADOW` on the implementation branch until Task 10 records its
  baseline; do not merge a generated template that leaves existing reviewers
  in a promotion dead-end.
- [ ] Add GOVERNANCE Non-Claims stating that manual prompt-reviewer results are
  point-in-time evidence, not CI automation or proof against unknown attacks.
- [ ] Add per-profile generated-validation assertions that
  `docs/REVIEWER_ASSURANCE.md` exists, carries the reviewer-state/promotion
  contract, and is linked from the rendered documentation index.

### Task 10: Run and record the initial reviewer baseline

**Files:**
- Modify: `docs/REVIEWER_ASSURANCE.md`
- Modify: `template/{{project_slug}}/docs/REVIEWER_ASSURANCE.md`

**Interfaces:**
- Consumes: exact reviewer prompt, seeded cases, and promotion rubric.
- Produces: attributable human-approved status per reviewer.

- [ ] Run deterministic cases locally. Expected: all vulnerable cases in scope
  detected and all safe near-misses non-blocking.
- [ ] Evaluate each existing prompt reviewer only against its declared domain,
  using a fresh isolated context and the exact prompt definition.
- [ ] Record per-case disposition and evidence quality; do not summarize a
  reviewer as passing without preserving the evaluated case IDs and the
  reviewed prompt-definition hash/version.
- [ ] Keep any reviewer that misses a required case or false-blocks a safe case
  in `SHADOW`; fix its prompt in a later focused change and rerun the complete
  domain set.
- [ ] Have the human maintainer approve each promotion record. Agent output
  alone cannot set `BLOCKING`.
- [ ] Mirror approved baseline records into the template assurance register so
  generated projects start with evaluated built-in reviewers. Any downstream
  material prompt/scope/tool/model change returns that reviewer to `SHADOW` and
  uses the shipped promotion procedure.
- [ ] Run all root unit tests, `python scripts/quick_checks.py`, and
  `bash scripts/validate_generated.sh`. Expected: all pass; inspect warnings.
- [ ] Request final Bugbot plus security-architect, SRE, and template-DX
  reviews. Re-test and re-review until every reviewer returns `APPROVED`.

## Program Completion

- [ ] Verify four focused PRs exist and no unrelated generated artifacts or
  local database files are included.
- [ ] Verify root and rendered template docs make only claims backed by code,
  tests, or recorded manual evidence.
- [ ] Verify all recurring findings raised during implementation went through
  the new bug-class decision and, where applicable, include test + rule +
  register links.
- [ ] Verify the maintainer has explicitly approved final reviewer promotions
  and required GitHub checks.
