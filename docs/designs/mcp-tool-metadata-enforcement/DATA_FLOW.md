# Data Flow: MCP Tool Metadata Enforcement

**Status:** Design artifact (High risk-tier) — REV 2

## Source of Truth

| Data | Source | Persistence |
|------|--------|-------------|
| Enforce toggle | Env | None |
| Findings | Layer 1+2 / hash compare | Integrity flags via flag_hook |
| Blocks | End-of-screen rebuild when enforce ON + screen OK | `blocked_tools` on registry JSON |
| Refuse to caller | call_tool / filtered list | API JSON (`error_code`) |

## list_tools

1. Transport fetch (full list).
2. `screen_listed_tools` in thread.
3. On success + enforce ON: assign `blocked_tools = new_blocks` (injection names only); filter return.
4. On success + enforce OFF: leave `blocked_tools` as persisted; return full list.
5. On outer screen exception: no write; full list; `metadata_screen_failed=1`.

## call_tool

1. API may 404 if `enabled=False` (before call).
2. If enforce ON and `tool_name in config.blocked_tools`: refuse result (no transport).
3. Else transport as today.

## Trust / failure table

| Failure | list_tools | call_tool |
|---------|------------|-----------|
| Enforce off | Full list | No metadata refuse |
| Enforce on, never screened | Full list | No refuse (empty blocks) |
| Enforce on, screen OK, injection | Filtered | Refuse blocked |
| Outer screen crash, enforce on | Full list + failed flag | Prior blocks still refuse |
| persist() fails after screen | In-memory blocks OK until restart | Post-restart may allow until next health |

## Cross-tenant

Each `MCPServerConfig` is tenant-scoped. Blocks discovered under tenant A never
apply to tenant B’s config. GOVERNANCE Non-Claim: no cross-tenant propagation.
