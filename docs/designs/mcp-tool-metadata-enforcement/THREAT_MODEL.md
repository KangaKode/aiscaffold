# Threat Model: MCP Tool Metadata Enforcement

**Status:** Design artifact (High risk-tier) — REV 2

## Assets

Tool metadata integrity; call_tool choke point; operator `enabled` control;
default-off listing; bounded refuse codes; per-tenant block isolation.

## Abuse Cases

### A1 — Poisoned description admitted
**Control:** Enforce on + post-screen → omit + refuse call.  
**Residual:** Pre-health window; drift-only; detector FN.

### A2 — Invoke by name after list filter
**Control:** call_tool checks blocked_tools when config passed.  
**Residual:** call_tool without config.

### A3 — Disabled-server oracle
**Control:** 404 detail byte-identical to not-found.  

### A4 — Always-on surprise / auto-disable
**Control:** Default off; never mutate `enabled`.  

### A5 — Toggle-window clear bypass
**Control:** REV 2 — no clear-on-off-screen; READ gated by flag.  

### A6 — Outer scanner crash under enforce
**Control:** list fail-open + prior blocks fail-safe on call; metadata_screen_failed.  

### A7 — Cross-tenant block bleed
**Control:** Per-tenant configs; Non-Claim.  

### A8 — Enrichment injects refuse text into prompts
**Control:** Log-and-continue; pin no enum in context.  

### A9 — ASI05/ASI10 overclaim
**Control:** GOVERNANCE + SECURITY_MAPPING honesty.  

### A10 — Partial mid-screen blocked_tools write
**Control:** Single end-of-screen assignment only.  

## Acceptance Criteria

1. Default off: full list; no metadata refuse; enabled 404 works.  
2. Enforce on + health + injection: filter + refuse with error_code.  
3. Drift alone never refused.  
4. enabled=False detail == not-found.  
5. Outer screen fail: full list + failed flag; prior blocks still refuse.  
6. No pattern text in blocked_tools / refuse fields.  
7. Enrichment refuse non-fatal, no prompt leak.  
8. Docs named edits land; no ASI05/ASI10 covered flip.  

## Residual (accepted)

Pre-health inert window; drift executable; bare list_tools; persist loss on
restart; Layer 1+2 FN/FP; not network containment.
