# Bug-Class Register

This register records every finding a human maintainer classifies as a
**recurring bug class**: the same failure could re-emerge in a
different diff because an invariant is missing or under-specified. It
is the audit destination for the closed-loop completion gate defined
in [`docs/DEVELOPMENT_PROCESS.md`](DEVELOPMENT_PROCESS.md) and the
always-applied `development-process` rule.

**Today the register is empty.** No historical bug classes have been
imported. Each new entry is added by a human maintainer as findings
land — agents may propose entries, but the classification, the
instruction update, and the register write are all human-gated.

## Schema

Each entry carries these fields:

| Field | Meaning |
|-------|---------|
| ID | Stable identifier, `BC-####` (bug-class number, monotonically assigned) |
| Date / Source | ISO date the class was recorded, and the finding that surfaced it (review verdict, CI job, incident, or issue) |
| Classification | `one-off` or `recurring class` — the human maintainer's decision |
| Invariant | The rule the bug violates, in one sentence (what must hold that did not) |
| Affected scope | Paths, layer, or subsystem this class applies to |
| Relevant rule / instruction | Path to the agent rule (`.cursor/rules/*.mdc`) or instruction (`.cursor/agents/*.md`, docs, or check script) that was updated to prevent recurrence |
| Regression test | Path to the test that fails on the pre-fix code and passes after the fix |
| Owner | Human maintainer accountable for the class |
| Status | `DRAFT`, `SHADOW`, `BLOCKING`, or `SUSPENDED` — same closed vocabulary as [`docs/REVIEWER_ASSURANCE.md`](REVIEWER_ASSURANCE.md) |

## Completion gate (recurring class)

A recurring bug-class fix cannot be approved without all three linked
artifacts in the same PR:

1. a **regression test** that fails on the pre-fix code and passes
   after the fix;
2. an **update to the nearest relevant agent rule or instruction**
   (not merely prose appended to this register); and
3. an **entry in this register** linking the source finding, the
   invariant, the rule change, and the regression test.

A one-off defect requires the regression test only and is not entered
here. Findings whose classification a human maintainer has not yet
decided stay in the register with status `DRAFT` — the vocabulary is
closed to `DRAFT`, `SHADOW`, `BLOCKING`, and `SUSPENDED`, matching the
[reviewer-assurance](REVIEWER_ASSURANCE.md) vocabulary.

## Authority boundary

- Agents may propose a classification, cite prior register entries,
  and draft an instruction edit — but they may not auto-classify,
  self-edit their own rules, or approve their own rule changes. A
  human maintainer performs the classification and the register
  write.
- Scoped reviewers (`.cursor/agents/*.md`) follow the same boundary
  defined in
  [`template/{{project_slug}}/.cursor/rules/expert-review.mdc`](../template/%7B%7Bproject_slug%7D%7D/.cursor/rules/expert-review.mdc):
  no merge, no fix, no self-edit-of-own-rules, no self-promotion.

## Non-claims

- **CI does not enforce the three-artifact gate.** It is a
  human/review gate. Documentation-parity tests only assert that this
  register, the process docs, and the always-applied rules reference
  each other; they do not judge whether a specific fix meets the
  three-artifact bar.
- **This register begins with no invented historical bug classes.**
  Do not back-fill entries to make the register look older than it
  is; add an entry the first time a human classifies a recurring
  class.
- **Deterministic scanners are outside this register.** Their
  behavior is governed by the shared proof-of-finding contract in
  [`template/{{project_slug}}/.cursor/rules/expert-review.mdc`](../template/%7B%7Bproject_slug%7D%7D/.cursor/rules/expert-review.mdc)
  and their stable rule IDs.

## Register

| ID | Date / Source | Classification | Invariant | Affected scope | Relevant rule / instruction | Regression test | Owner | Status |
|----|---------------|----------------|-----------|----------------|-----------------------------|-----------------|-------|--------|
| _(none yet)_ | | | | | | | | |

Guidance verified: 2026-07
