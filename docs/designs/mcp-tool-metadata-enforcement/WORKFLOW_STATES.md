# Workflow States: MCP Tool Metadata Enforcement

**Status:** Design artifact (High risk-tier) — REV 2

## Operator states

| Flag | Screen | Persist blocks | Filter list | Refuse call | Invoke disabled |
|------|--------|----------------|-------------|-------------|-----------------|
| unset | detect-only | no write | no | no | 404 (new) |
| on, pre-health | detect-only | no (empty) | no | no | 404 |
| on, post-health | detect+write | yes | yes | yes | 404 |

## GOVERNANCE edits (named targets)

1. **Capability row** `template/.../docs/GOVERNANCE.md` “Ingestion injection detection”
   — replace mid-sentence “never refuse/disable tools by default” with:
   “refuse/disable remains off unless `MCP_TOOL_METADATA_ENFORCEMENT_ENABLED`
   (injection-flagged tools only; drift stays detect-only).”

2. **New Non-Claim bullet** (same posture shape as runtime canary / Sentinel):
   Opt-in MCP metadata enforcement is off by default. When on, injection-flagged
   tool metadata is omitted from `list_tools` and `call_tool` returns the
   **stable enum** `error_code`/`error_message`=`tool_refused_metadata_injection`
   before transport — only after a successful list_tools screen against that
   server’s config (health today). Drift never refuses. Outer screen failure
   leaves prior blocks for call_tool but returns an unfiltered list
   (`metadata_screen_failed`). Bare list_tools without config cannot refuse.
   Disabled servers get invoke 404 identical to missing. Blocks are per-tenant
   config (no cross-tenant propagation). Persist failure may drop blocks across
   restart. Not ASI05 egress / ASI10 policy tables. Never auto-sets
   `enabled=False`.

3. **SECURITY_MAPPING.md ASI02** — parallel one-clause update: opt-in enforce
   covers injection metadata only; default remains detect-only.

## OPERATIONS runbook (new section sketch)

- Flag meaning; enable only after accepting tool disappearance on FP descriptions.
- Recovery: fix supplier text → re-run health → blocks rebuild.
- All-refused: `healthy=false`, `tools_available=0`, `tools_refused=N`.
- Inspect: registry `blocked_tools`, integrity flag `mcp_tool_metadata_injection`.
- Kill switch: unset flag (blocks inert immediately; no clear required).
- After enabling flag: call each server’s health before relying on refuse.

## PLATFORM_GUIDE / .env.example

- MCP connectors paragraph: opt-in enforce invariants + link GOVERNANCE.
- `.env.example.jinja` MCP block: commented flag + pointer to GOVERNANCE
  (mirror RUNTIME_CANARY / SENTINEL pattern).

## Test pins (replace `test_no_refuse_disable_env_in_tool_screen_source`)

1. Flag unset: list set byte-identical; call_tool never refuse enum; no
   `enabled` mutation by screen/client.
2. Flag on + injection: omitted from list; call_tool refuse before transport;
   `error_code` exact; HTTP invoke 200+is_error.
3. Drift-only: not refused.
4. `enabled=False` invoke: 404 detail == missing string.
5. Outer screen raise: full list + metadata_screen_failed; prior blocks still
   refuse call_tool.
6. End-of-screen atomicity: mid-failure leaves prior blocked_tools.
7. Legacy registry JSON without `blocked_tools` loads.
8. Enrichment refuse: non-fatal; prompt context lacks refuse enum.
9. Transport error path never emits the refuse enum string.
10. Cross-tenant: tenant A blocks do not affect tenant B config.
11. Health: enforce-on successful screen → `tools_refused == len(new_blocks)`;
    flag unset → omitted or zero.
12. Python `MCPToolResult`: `.is_error is True` and
    `.error_code == "tool_refused_metadata_injection"` (library callers, not
    only HTTP).
