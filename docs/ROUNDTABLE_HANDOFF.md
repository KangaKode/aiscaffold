# Roundtable POC Handoff

Use this checklist when a POC uses the round table, chat orchestrator, or
external agent protocol and needs engineering review. It is specific to
multi-agent orchestration; use your project handoff packet for general setup,
security, data, and release evidence.

## Handoff Summary

Before review, document:

- Orchestrator entry point:
- Chat endpoint or in-process caller:
- Round table endpoint or in-process caller:
- Agent registry location:
- Local agents:
- Remote agents:
- Required model/provider env vars:
- Demo task:
- Expected output:
- Known production blockers:

## Agent Protocol Review

For every local or remote agent, record:

| Check | Evidence |
|-------|----------|
| Agent name and domain are clear | |
| `/analyze` or in-process analysis returns evidence-backed observations | |
| `/challenge` or challenge method can disagree with counter-evidence | |
| `/vote` or vote method returns approve/dissent plus conditions | |
| Timeout behavior is known | |
| Invalid JSON or malformed output behavior is known | |
| Response size limits are known | |
| Agent does not receive data outside its scope | |
| Auth/API key expectations are documented for remote agents | |

Review `AGENT_PROTOCOL.md` for the HTTP contract.

## Orchestration Evidence

Capture one representative run with:

- Task id:
- Input prompt or task:
- Strategy/focus areas, if used:
- Agents selected:
- Independent analyses produced:
- Challenge phase completed:
- Synthesis generated:
- Votes recorded:
- Dissent or minority view preserved:
- Evidence enforcement result:
- Final response or artifact:

If any phase is skipped for the POC, record why it is safe for the demo.

## Failure Behavior

Document what happens when:

- An agent times out.
- An agent returns invalid JSON.
- An agent returns unsupported claims.
- An agent refuses or flags out-of-domain scope.
- A remote agent is unavailable.
- The LLM provider key is missing.
- The evidence pipeline downgrades or rejects output.

At least one failure path should be tested or manually demonstrated before an
engineer takes over a multi-agent POC.

## Observability And Debugging

Record where reviewers can inspect:

- Per-agent request/response summaries.
- Phase transitions.
- Timing and timeout evidence.
- Token/cost usage, if LLM calls are enabled.
- Evidence-level warnings or weak-output findings.
- Agent health or registration state.
- Any generated artifacts.

Do not include full prompts, secrets, API keys, PII, or customer data in logs or
handoff notes.

## Scaffold Boundary Checks

Before handoff, confirm:

- The POC did not bypass the orchestrator with direct agent-to-agent calls.
- LLM access goes through the scaffold client, not scattered direct SDK calls.
- User or tenant context is carried through orchestration paths where applicable.
- Remote agent URLs are validated before use.
- Evidence levels and citations are not presented as stronger than they are.
- Demo-only shortcuts are listed as production blockers.

## Minimum Signoff

A roundtable-based POC is engineer-review ready when:

- At least one successful run is captured.
- At least one failure behavior is documented.
- Agent contracts are reviewed.
- Observability evidence is available.
- Production blockers are explicit.
- Engineering and security reviewers can reproduce or inspect the run.
