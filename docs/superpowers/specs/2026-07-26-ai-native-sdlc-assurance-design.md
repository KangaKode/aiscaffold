# AI-Native SDLC Assurance Design

**Date:** 2026-07-26
**Status:** Approved design; implementation not started
**Source:** Anthropic, “How Anthropic secures its AI-native software development lifecycle”

## Goal

Add five mutually reinforcing controls to roundtable’s development process and
generated scaffold:

1. feed recurring bug classes back into agent instructions;
2. block secrets and known-vulnerable dependencies in CI;
3. require proof for blocking reviewer findings;
4. key design ceremony to explicit risk tiers; and
5. test reviewers with seeded cases and shadow-run new reviewers.

Human ownership, explicit approval, and the existing “detect, never auto-act”
constraint remain unchanged.

## Scope

The change applies to both this repository’s own development process and the
process assets shipped by the Copier template. It changes governance documents,
Cursor rules and reviewer prompts, CI workflows, deterministic review tooling,
and tests. It does not add a hosted review service, an LLM runner to GitHub
Actions, remote development VMs, continuous DAST, or automatic rule rewriting.

## Risk-Tier Policy

The highest applicable tier wins. A small diff cannot be downgraded when it
touches a high-risk path or invariant.

### High

Examples include security and authentication, agent identity or permissions,
enforcement, migrations/schema/RLS, tenant or learning data, secrets and
deployment boundaries, CI workflows, and hooks.

Required before implementation:

- architecture map;
- data-flow diagram;
- workflow states or wireframes; and
- threat model.

High-tier changes always receive all relevant domain reviews.

### Medium

Medium is the default for every change that is neither High nor Low. Examples
include single- or multi-file behavioral/product changes that do not alter a
high-risk invariant.

Required before implementation: one concise design note covering architecture
impact, data movement, failure behavior, risks, and planned tests. It may be
split into two documents when that makes review materially clearer.

### Low

Examples include documentation and presentation-only work, test-only changes,
and fixes with fewer than 20 gross changed lines (additions plus deletions,
excluding mechanically generated artifacts) that do not touch high-tier paths
or invariants.

Low-tier work is exempt from design artifacts, not from branch isolation,
applicable tests, CI, human ownership, or post-change review. The maintainer
records the tier and rationale in the PR description when invoking the
exemption.

## Closed-Loop Bug-Class Feedback

Every confirmed review, CI, or incident finding is classified as either a
one-off defect or a recurring bug class.

A recurring class is not complete until the same PR contains:

1. a regression test that fails on the vulnerable behavior;
2. an update to the relevant agent rule or instruction;
3. an audit-register entry linking the source finding, protected invariant,
   prevention rule, and regression test.

The process is human-gated. Agents may propose classifications and edits, but
must not rewrite their own instructions or approve the result automatically.
Rules that affect generated projects are updated in both root governance and
the template counterpart.

## CI Security Scanning

A dedicated blocking security job will:

- run Gitleaks over repository content and relevant Git history;
- run `pip-audit` against the root `core/` project;
- run `pip-audit` against generated projects’ resolved base, development, and
  optional dependency set; and
- retain the existing Bandit and project-specific security checks.

Secret and dependency findings block immediately. CI never auto-fixes,
suppresses, or upgrades a dependency. Gitleaks and `pip-audit` are pinned in CI.
Before the blocking jobs land, a read-only preflight must establish a clean
baseline or route a finding into a separate remediation PR.

### Secret-Scanning Baseline

The 2026-07-26 preflight found no real secrets, but Gitleaks matched three
deliberate fake credentials in template test and eval fixtures. Because those
files render into every generated project, an unaddressed baseline would fail
downstream CI on its first run.

The baseline is cleaned at the source rather than suppressed by policy:
fixture values are stored as fragments that do not match Gitleaks in
repository text and are assembled in memory by the consuming test or eval
harness, preserving each fixture’s original semantics. Generated projects
therefore ship **no** Gitleaks allowlist or ignore file; a downstream first
run is clean because the fixtures are clean.

Commits already in this repository’s history cannot be edited, so the root
repository — and only the root repository — carries a `.gitleaksignore` listing
the exact historical fingerprints, one commented line each. Fingerprints are
commit-scoped, so this cannot mask a future secret, including one reintroduced
on the same line. Root-level allowlisting by rule, path glob, directory, or
value regex is out of scope: it would suppress unrelated future findings.

Dependency-advisory exceptions use a machine-readable allowlist consumed by a
fail-closed wrapper. Every entry includes advisory ID, reachability rationale,
owner, compensating control, and ISO expiry date. Malformed or expired entries
fail before `pip-audit` runs; active entries become explicit
`--ignore-vuln` arguments. Tests prove expiry restores blocking behavior.
There is no blanket scanner bypass or permanent exception.

Because dependency ranges are not lock files, the audit proves that the
currently resolved set is free of known advisories; it does not prove
reproducibility. The documentation must preserve this limitation.

## Proof-of-Finding Contract

Only verified findings may block.

Agentic security findings must include:

- attacker-controlled source;
- dangerous sink or violated invariant;
- reachable path between them;
- concrete trigger or reproduction;
- challenge against existing defenses;
- impact and exact location; and
- specific remediation.

General correctness findings must identify the failing execution path or
invariant and provide reproducible evidence. Deterministic scanners must emit a
stable rule ID, exact location, and matched evidence. A concern that cannot meet
the applicable standard is labeled `UNVERIFIED`; it may be reported for
follow-up but cannot block or count toward a clean-slate target.

## Reviewer Assurance and Shadow Mode

Seeded vulnerable fixtures and safe near-misses will cover at least:

- hardcoded secrets;
- SQL injection;
- unsafe shell execution;
- path traversal;
- missing auth or tenant scope;
- prompt-injection boundary violations.

The fixture corpus and deterministic runner ship in generated projects, along
with a domain coverage manifest that labels each case `DETERMINISTIC` or
`MANUAL_AGENT`. Documentation and CI claims must preserve that distinction.
Deterministic reviewer tests run on every PR. New or materially changed
prompt-based reviewers begin in `SHADOW`: they can comment but cannot block.
Promotion to blocking status requires passing the seeded vulnerable cases,
producing no false blocking result on safe fixtures, human review of evidence
quality, and a recorded approval. Existing reviewers receive an initial
baseline evaluation; approved results are recorded in both the root and
generated assurance registers so downstream projects do not start in a shadow
dead-end.

GitHub CI has no authenticated Cursor-agent runner. Therefore deterministic
reviewer evaluation is automated, while prompt-based reviewer shadow
evaluation is a documented manual gate. The project must not claim otherwise.
An always-applied reviewer rule consults the assurance register: only reviewers
recorded as `BLOCKING` may recommend a block.

## Failure Behavior

- Scanner execution errors fail closed; they are not treated as clean scans.
- Malformed or expired dependency exceptions fail closed.
- Confirmed Gitleaks, `pip-audit`, Bandit, or deterministic reviewer findings
  fail the relevant CI job.
- Unverified agentic findings do not block, but remain visible for human triage.
- A reviewer stays in shadow mode until every promotion criterion is met.
- Generated validation must render and inspect workflow, documentation,
  reviewer-eval, and `.cursor` assets; source-text checks alone are
  insufficient.
- A bug-class fix missing its rule update, regression test, or register entry
  is incomplete and cannot receive final approval.

## Validation

Implementation will be test-first and split into focused PRs:

1. risk tiers and the threat-model artifact;
2. blocking secret and dependency scanning;
3. proof schema and bug-class feedback loop;
4. reviewer assurance fixtures and shadow protocol.

Each PR must pass `bash scripts/validate_generated.sh`, including inspection of
warnings, then receive Bugbot and the matching domain review. No implementation
or review agent may merge or deploy.

## Design Artifacts

- [Architecture Map](../../designs/ai-native-sdlc-assurance/ARCHITECTURE_MAP.md)
- [Data Flow](../../designs/ai-native-sdlc-assurance/DATA_FLOW.md)
- [Workflow States](../../designs/ai-native-sdlc-assurance/WORKFLOW_STATES.md)
- [Threat Model](../../designs/ai-native-sdlc-assurance/THREAT_MODEL.md)
