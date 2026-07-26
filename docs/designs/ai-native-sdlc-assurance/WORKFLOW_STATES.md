# Workflow States: AI-Native SDLC Assurance

**Status:** Design artifact
**Parent spec:** [AI-Native SDLC Assurance Design](../../superpowers/specs/2026-07-26-ai-native-sdlc-assurance-design.md)

## Change Workflow

### 1. Unclassified

The requested change has not yet been assigned a risk tier.

Exit condition: changed paths, affected invariants, and intended behavior are
understood well enough to choose the highest applicable tier.

### 2. Tiered

The change is marked High, Medium, or Low with a short rationale. Medium is the
default when neither High nor Low applies.

- High moves to `Designing (Full)`.
- Medium moves to `Designing (Concise)`.
- Low moves to `Exemption Recorded`.

Any later discovery of a higher-risk path or invariant raises the tier and
invalidates an insufficient design exemption.

### 3. Designing (Full)

All four documents are drafted: architecture map, data flow, workflow states or
wireframes, and threat model.

Exit condition: all required domain reviewers return `APPROVED`.

### 4. Designing (Concise)

One short note covers architecture impact, data flow, failure behavior, risks,
and tests.

Exit condition: the relevant reviewer returns `APPROVED`.

### 5. Exemption Recorded

The maintainer records why the change qualifies as Low in the PR description.
For the size-based path, “under 20” means gross additions plus deletions,
excluding mechanically generated artifacts. This exempts design artifacts only.

Exit condition: branch and test obligations are identified.

### 6. Implementing

Tests are written first for behavioral changes, then the smallest production or
configuration change is made.

Exit condition: focused tests pass and the change is ready for full validation.

### 7. Validating

The generated-project suite, deterministic reviewer fixtures, Gitleaks,
`pip-audit`, Bandit, and relevant tests run.

- A scanner error or confirmed finding returns the change to `Implementing`.
- A clean run advances to `Reviewing`.

### 8. Reviewing

Bugbot and matching domain experts review the actual diff using the
proof-of-finding contract.

- `UNVERIFIED` concerns are triaged but do not block.
- Confirmed findings return the change to `Implementing`.
- Confirmed recurring classes also enter `Bug-Class Feedback`.

### 9. Bug-Class Feedback

The same PR gains a regression test, relevant instruction update, and register
entry.

Exit condition: all three are present and re-review returns `APPROVED`.

### 10. Human Approval

The maintainer reviews code, scanner results, reviewer evidence, exceptions,
and any bug-class classification.

Exit condition: explicit approval. Agents cannot transition a change out of
this state.

### 11. Merge Eligible

All required checks and human gates are complete. External branch protection
may enforce additional conditions.

## Reviewer Lifecycle

### Draft

The reviewer definition is being created or materially changed. It has no
blocking authority.

### Shadow

The reviewer may analyze real diffs and seeded fixtures and may post clearly
labeled comments. Its verdict cannot block.

Exit criteria:

- detects every required seeded vulnerability in its domain;
- does not block safe near-miss fixtures;
- provides complete evidence for every proposed blocking finding;
- resists instructions embedded in fixtures and diffs;
- has a recorded human evaluation and approval.

### Blocking

The reviewer may contribute blocking recommendations within its declared
scope. Human approval remains final.

Any material prompt, model-behavior, tool, or scope change returns it to
`Shadow`. Editorial changes that cannot affect behavior do not.

### Suspended

A reviewer that misses a required seeded case, produces a false blocking
finding, follows injected instructions, or exceeds its scope loses blocking
status until corrected and re-evaluated.

## Exception States

### Advisory Exception Requested

A dependency advisory is believed not to affect the project. The request must
identify the advisory, affected package, reachability argument, owner, expiry,
and compensating control.

### Advisory Exception Active

The exception is explicit, reviewable, and temporary. Expiry returns the
finding to blocking status. A CI wrapper validates required fields and expiry,
then supplies the advisory ID as an explicit ignore argument. The scanner is
not globally disabled.

### Scanner Unavailable

Network, database, action, or execution failure prevents a trustworthy result.
The job fails closed and must be rerun; unavailability is not a clean scan.
