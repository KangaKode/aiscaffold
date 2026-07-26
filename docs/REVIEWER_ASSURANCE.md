# Reviewer Assurance Register

This register records the assurance status of every prompt reviewer that
ships with `roundtable`. It is the single source of truth that gates
whether a prompt reviewer may recommend `BLOCK` on a change — the
always-applied `red-team.mdc` rule and the on-demand `expert-review.mdc`
protocol consult this file before acting on any blocking recommendation.

This register is a governance stub. It ships in this state so the
consultation contract has a real destination on day one; **it does not
imply that CI evaluates prompt reviewers, and it does not record any
promotion history**. The promotion protocol and the reviewer-baseline
fixture land in a later change (PR 4).

## Assurance States

- **`DRAFT`** — reviewer definition exists but has not been exercised
  against the reviewer-baseline fixture. May not recommend `BLOCK`.
- **`SHADOW`** — reviewer runs on every applicable change and reports
  findings, but its `BLOCK` recommendations are advisory only. `SHADOW`
  is the default entry point for a newly-authored or newly-modified
  prompt reviewer. May not recommend `BLOCK`.
- **`BLOCKING`** — reviewer has cleared the promotion protocol against
  the reviewer-baseline fixture and may recommend `BLOCK`. Promotion
  records live alongside this table once PR 4 lands.
- **`SUSPENDED`** — reviewer was previously `BLOCKING` but has been
  demoted (regression, drift, or maintainer decision). May not recommend
  `BLOCK` until re-promoted.

**Today no prompt reviewer may recommend a block.** Every prompt
reviewer listed below is `SHADOW`. Deterministic scanners
(`scripts/agent_review.py`, `template/{{project_slug}}/scripts/red_team_check.py`)
are not prompt reviewers and are not governed by this register — their
exit-code semantics stand.

## Register

| Reviewer                          | Kind          | File                                                          | Version | Status   |
|-----------------------------------|---------------|---------------------------------------------------------------|---------|----------|
| red-team (always-applied rule)    | prompt rule   | `template/{{project_slug}}/.cursor/rules/red-team.mdc`        | v0      | `SHADOW` |
| red-team (agent)                  | prompt agent  | `template/{{project_slug}}/.cursor/agents/red-team.md`        | v0      | `SHADOW` |
| sast-reviewer                     | prompt agent  | `template/{{project_slug}}/.cursor/agents/sast-reviewer.md`   | v0      | `SHADOW` |
| security-hardener                 | prompt agent  | `template/{{project_slug}}/.cursor/agents/security-hardener.md` | v0    | `SHADOW` |
| agent-security-specialist         | prompt agent  | `template/{{project_slug}}/.cursor/agents/agent-security-specialist.md` | v0 | `SHADOW` |
| code-reviewer                     | prompt agent  | `template/{{project_slug}}/.cursor/agents/code-reviewer.md`   | v0      | `SHADOW` |
| solution-architect                | prompt agent  | `template/{{project_slug}}/.cursor/agents/solution-architect.md` | v0   | `SHADOW` |
| test-architect                    | prompt agent  | `template/{{project_slug}}/.cursor/agents/test-architect.md`  | v0      | `SHADOW` |
| data-flow-guardian                | prompt agent  | `template/{{project_slug}}/.cursor/agents/data-flow-guardian.md` | v0   | `SHADOW` |

"Version" is a maintainer-assigned label pinned to a specific reviewer
prompt. When a reviewer's rule or agent file changes, its assurance
status resets to `SHADOW` and the version is bumped.

## Non-claims

- This register does not claim that CI enforces prompt-reviewer
  promotion. There is no automated promotion pipeline in this change;
  the maintainer edits this file by hand.
- This register does not record any historical promotion or demotion.
  All entries are the initial `SHADOW` state.
- Deterministic scanners are outside this register. Their behavior is
  governed by the shared proof-of-finding contract in
  `template/{{project_slug}}/.cursor/rules/expert-review.mdc` and their
  stable rule IDs.

## Related contracts

- Blocking-evidence contract:
  `template/{{project_slug}}/.cursor/rules/expert-review.mdc` — Proof of
  Finding section (six required fields, correctness branch, and
  `UNVERIFIED` label).
- Always-applied consultation:
  `template/{{project_slug}}/.cursor/rules/red-team.mdc` — the
  "Assurance Register Gate" section.

Guidance verified: 2026-07
