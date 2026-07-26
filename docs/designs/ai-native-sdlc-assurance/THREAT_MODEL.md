# Threat Model: AI-Native SDLC Assurance

**Status:** Design artifact
**Parent spec:** [AI-Native SDLC Assurance Design](../../superpowers/specs/2026-07-26-ai-native-sdlc-assurance-design.md)

## Assets

- integrity of source code and generated scaffold output;
- repository and CI credentials;
- dependency supply-chain integrity;
- accuracy and independence of review gates;
- agent instruction integrity;
- auditability of findings, approvals, exceptions, and reviewer promotion;
- tenant, learning, authentication, and agent-identity invariants protected by
  the development process.

## Actors

- human maintainer and reviewers;
- implementation and review agents;
- GitHub Actions and third-party actions;
- PyPI, vulnerability databases, and dependency publishers;
- contributors supplying diffs, issues, fixtures, and documentation;
- attackers able to place prompt-injection text, malicious code, secrets, or
  poisoned dependencies in content the workflow consumes.

## Trust Boundaries

1. Untrusted repository content enters deterministic and agentic review.
2. Third-party actions and vulnerability data enter CI.
3. Agent recommendations cross into human decision-making.
4. Root template sources render into downstream generated repositories.
5. A confirmed defect crosses into persistent agent guidance through the
   bug-class loop.
6. A shadow reviewer crosses into blocking status through human promotion.

## Threats and Controls

### Prompt-injected reviewer

**Threat:** A diff, comment, fixture, or tool output instructs a reviewer to
ignore a vulnerability, leak data, or approve the change.

**Controls:** Reviewer definitions treat all reviewed content as untrusted;
seeded fixtures include injection attempts; prompt reviewers begin in shadow;
multiple scoped reviewers and human approval remain independent gates.

**Residual risk:** A sufficiently capable injection may evade seeded cases and
influence multiple reviewers. Reviewers receive separate contexts where
possible, and humans inspect evidence rather than bare verdicts.

### False blocking finding

**Threat:** An agent produces a plausible but unproven claim that stops work or
causes an unsafe “fix.”

**Controls:** Blocking findings require source/path/sink or violated-invariant
proof, reproduction, defense challenge, exact location, and impact.
`UNVERIFIED` findings cannot block. Safe near-miss fixtures test specificity.

**Residual risk:** Evidence can be internally consistent but wrong. Human and
independent reviewer checks remain required for high-risk findings.

### Reviewer misses malicious change

**Threat:** A reviewer prompt, model, or deterministic rule has a blind spot.

**Controls:** Multiple narrow reviewers, seeded vulnerable cases, regression
fixtures for discovered misses, shadow mode after material reviewer changes,
and deterministic SAST/secret/dependency checks.

**Residual risk:** Fixtures cover known classes, not unknown vulnerabilities.
The suite grows through the closed-loop bug-class process.

### Reviewer self-promotion or self-modification

**Threat:** An agent updates its own instructions, marks itself blocking, or
approves the rule intended to constrain it.

**Controls:** Rule changes and reviewer promotion require explicit human
approval and recorded evidence. Review agents are read-only. CI does not
automatically rewrite instructions.

**Residual risk:** A human may rubber-stamp an agent’s proposed update. Domain
review and seeded evidence reduce, but do not eliminate, this risk.

### Secret committed to repository

**Threat:** Credentials enter current content or reachable Git history.

**Controls:** Gitleaks is a blocking CI job; existing heuristic checks and
`.gitignore` remain defense in depth; findings require human-led revocation and
history remediation.

**Residual risk:** Novel secret formats or encrypted/encoded credentials may
evade detection. Gitleaks does not prove that no secret exists.

### Vulnerable or poisoned dependency

**Threat:** A direct or transitive package contains a known vulnerability or a
malicious release.

**Controls:** Blocking pinned `pip-audit`, Dependabot, constrained dependency
declarations, audit of generated base/development/optional dependencies, and
review of dependency changes. Exceptions are machine-readable, expiring, and
fail closed when malformed or stale.

**Residual risk:** `pip-audit` detects published advisories, not malicious
packages without advisories. Version ranges are not reproducible lock files.

### Scanner bypass or silent failure

**Threat:** A tool crashes, loses network access, scans the wrong path, or is
removed from the required-check summary while CI appears green.

**Controls:** Scanner errors fail closed; action and auditor versions are
pinned; workflow tests assert job and summary wiring; generated validation
verifies rendered workflows and `.cursor` assets; branch protection is updated
to require the security result.

**Residual risk:** Branch protection is external configuration and cannot be
proven solely from repository files.

### Seed fixture trips secret scanning

**Threat:** A deliberately vulnerable reviewer fixture resembles a credential
and permanently blocks Gitleaks.

**Controls:** Secret fixture values are stored as low-entropy, non-matching
fragments and assembled only in the in-memory reviewer harness. No usable or
Gitleaks-matching token is committed. Tests verify both reviewer detection and
Gitleaks-safe source representation.

**Residual risk:** Future fixture edits may introduce a matching literal; the
blocking Gitleaks job then fails visibly.

### Risk-tier downgrade

**Threat:** A small or documentation-framed change alters a high-risk invariant
while claiming Low ceremony.

**Controls:** Highest applicable tier wins; path and invariant triggers override
line count; later discoveries raise the tier; maintainer records Low-tier
rationale.

**Residual risk:** Semantic effects may not be obvious from changed paths.
Architecture and domain review can reclassify before merge.

### Bug-class guidance poisoning

**Threat:** A false or overbroad finding becomes permanent agent guidance and
causes recurring bad changes.

**Controls:** Only confirmed findings enter the loop; each class links to a
failing regression test and relevant invariant; human and domain review approve
the rule update; the register preserves provenance.

**Residual risk:** A valid rule may become stale as architecture changes.
Guidance carries verification dates and is re-evaluated when affected systems
change.

## Security Acceptance Criteria

- No scanner or reviewer may silently alter source, dependencies, prompts, or
  history.
- Secret and known-vulnerability findings block immediately.
- Scanner execution errors do not produce success.
- No unverified agentic finding has blocking authority.
- No new or materially changed prompt reviewer starts as blocking.
- No recurring bug-class fix closes without test, rule, and register links.
- High-tier changes cannot use size-based exemptions.
- Documentation states the limits of range-based dependency audits and manual
  prompt-reviewer evaluation.
- Generated validation proves rendered workflow, documentation, reviewer-eval,
  and `.cursor` assets rather than checking only template source.
