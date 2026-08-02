# Design Note: done-claim-verifier

**Risk-tier:** Medium  
**Status:** Implementation companion for the done-claim verifier agent

## Intent

Ship a readonly Cursor subagent that skeptically checks work claimed as done
(implementation exists, relevant tests or project checks were run, gaps
reported). It is an implementer hygiene gate, orthogonal to
`REVIEWER_ASSURANCE` security reviewers and to product Sentinel.

## Architecture impact

- New prompt agent only:
  `template/{{project_slug}}/.cursor/agents/done-claim-verifier.md`
- Process docs and always-applied `development-process` rules gain one
  additive bullet: invoke the agent after implementation before claiming
  complete. It does not replace code review, red-team, CI, or Bugbot.
- No register row, no reviewer-eval cases, no CI workflow or hook changes.

## Data movement

None. The agent reads the workspace and runs local tests or validation
commands; it does not write merge commits, assurance docs, or `.cursor/**`
edits as part of its authority.

## Failure behavior

- Report incomplete when claimed artifacts are missing, tests were not run,
  or checks fail.
- Report pass only when the claim is evidenced.
- On ambiguity, report gaps rather than inventing a pass.
- Never merge, never open fix commits under its own authority, never edit
  its own prompt or assurance docs.

## Risks

| Risk | Mitigation |
|------|------------|
| Name collision with product Sentinel | Agent id `done-claim-verifier`; process docs state the distinction |
| Mistaken promotion to assurance BLOCKING | Explicit non-claim in agent + process; no register row in this change |
| Scope creep into auto-fix | `readonly: true`; authority section forbids merge/fix/self-edit |
| Treated as substitute for security review | Process: additive only |

## Planned tests

- Agent file present with `name: done-claim-verifier` and `readonly: true`
- Authority strings present (no merge / no fix commits / no self-edit of
  `.cursor/**` or assurance docs)
- Process docs and rules mention the agent
- `validate_generated.sh` exists/has checks for all profiles
- Unit pins under `tests/test_development_process.py` (or a small sibling
  module)
