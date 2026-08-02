# Architecture Map: MCP Tool Metadata Enforcement

**Status:** Design artifact (High risk-tier) — REV 2  
**Risk-tier:** High (opt-in enforcement on MCP discovery/execution).

## PM fold-in

- Trigger-2 post-#111: **KEEP-ORDER**
- Trigger-1: **Ship A** — injection-only refuse; drift advisory; fold
  `enabled=False`→404; never auto-mutate `enabled`

## Env

`MCP_TOOL_METADATA_ENFORCEMENT_ENABLED` — `true`/`1`/`yes`; **default off**.

## Behavior pins (REV 2)

### Write vs read of `blocked_tools`

- **READ** (filter list / refuse call_tool): only when `enforcement_enabled()`.
- **WRITE**: only when enforce ON **and** the outer `screen_listed_tools` call
  completes without raising. Single end-of-screen assignment:
  `config.blocked_tools = new_blocks` (rebuild from this screen’s injection
  findings). Per-tool scanner exceptions fail-open for listing but **retain**
  a prior `metadata_injection` block for that named tool (never re-verified
  clean). Outer raise → prior `blocked_tools` **untouched**.
- Enforce OFF: **never write/clear** `blocked_tools` on screen (stale map is
  inert because READ is gated). No toggle-window destructive clear.

### Outer-screen exception (enforce ON)

- `list_tools`: fail-open → return **full** unfiltered list; set
  `report_out["metadata_screen_failed"]=1`.
- `call_tool`: prior `blocked_tools` still authoritative (fail-safe execute).
- Documented listing/execute asymmetry (Non-Claim).

### Enforcement effectiveness Non-Claim

`blocked_tools` is populated only via `list_tools`→screen (today: health
route). Invoke/enrichment never trigger screen. After enabling the flag,
operators must hit server health (or otherwise list_tools with config)
before refuse is effective. First-invoke / boot screen → out of scope.

### Injection-only refuse

- Filter omitted tools from `list_tools` return when enforce ON + config.
- `call_tool(..., config=)` before transport: if tool in `blocked_tools` →
  `MCPToolResult(is_error=True, error_message="tool_refused_metadata_injection",
  error_code="tool_refused_metadata_injection")`. Audit `status=refused_metadata`.
- Drift never enters `blocked_tools`.
- Bare list_tools without config: cannot refuse (Non-Claim).

### API invoke

- `enabled=False`: HTTP **404** with detail **byte-identical** to missing:
  `MCP server '{name}' not found` (no “disabled” oracle).
- Pass `config=` into `call_tool`.
- Refuse: **HTTP 200** + `is_error=true` + same `error_message` /
  `error_code` enum (never 403/500). Transport errors must never emit that
  exact enum string.

### Enrichment

- Pass `config=` into `call_tool`. On refuse: log-and-continue (non-fatal);
  refuse enum / error text **must not** enter prompt/context bytes.

### Persistence

- `MCPServerConfig.blocked_tools: dict[str, str]` reason enum value
  `metadata_injection` only.
- Load: missing→`{}`; non-dict→`{}`+warn; unknown reasons dropped.
- Pre-existing registry JSON without field loads cleanly.
- `registry.persist()` failure after screen may lose blocks across restart
  (Non-Claim / ops note).

### Health

- `tools_refused`: count of tools blocked **this successful enforce-on
  screen** (size of new_blocks). Omit/zero when enforce off.
- `healthy = len(returned_tools) > 0` after filtering; docstring updated
  (reachability alone no longer sufficient under enforce on).

### Line caps (same PR)

| File | New cap |
|------|---------|
| `tool_screen.py` | 230 |
| `mcp_client.py` | 290 |
| `api/routes/mcp.py` | 340 |

## Out of scope

Drift enforcement; auto `enabled=False`; first-invoke screen; ASI10; ASI05.

## Implementation order

Artifacts APPROVED → tests → screen/config/client/API/enrichment → docs → validate.
