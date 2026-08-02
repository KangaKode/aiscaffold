# Threat Model: Runtime Canary Enforcement

**Status:** Design artifact (High risk-tier) — REV 2

## Assets

- Security-boundary signal (canary non-echo)
- Caller-visible API/SSE responses (no token / detector internals)
- Integrity-flag audit trail (bounded, cooldown-safe)
- Byte-identical defaults when detection unset (including resolve still wrapped)
- Tenant isolation of `canary_leak` flags

## Actors

External API clients, LLM providers, operators, prompt-injection attackers,
buggy/slow stores.

## Trust Boundaries

1. Operator env → process  
2. Orchestrator → LLM (opt-in canary embed)  
3. LLM → observe  
4. Observe → store (off-loop, allowlisted detail)  
5. Orchestrator → HTTP/SSE client  

## Abuse Cases and Controls

### A1 — Model echoes canary

**Control:** Exact-substring detect; detect-only flag; opt-in refuse clears
content.  
**Residual:** Paraphrase FN (Non-Claim).

### A2 — Silent always-on prompt mutation

**Control:** Detection default off; per-surface off-path pinned (resolve stays
XML-wrapped without canary when off).  
**Residual:** Operator enables without reading ops docs.

### A3 — Enforce without detect

**Control:** `enforcement_enabled()` requires detection.  

### A4 — Token exfil via API / flags / logs

**Control:** `detail_json` allowlist `{"surface"}` only; `subject_id=surface`;
refuse enums only; `check_canary` warning does not echo token; tests pin API
JSON.  

### A5 — Global canary race

**Control:** Per-call local token only.  

### A6 — Store failure crashes or stalls requests

**Control:** `asyncio.to_thread` + swallow; refuse/deliver does not depend on
flag success.  

### A7 — Overclaim ASI05/ASI10

**Control:** GOVERNANCE Non-Claim text pinned in WORKFLOW_STATES; mapping rows
unchanged.  

### A8 — SSE streams leaked content on refuse

**Control:** Pinned wire order: skip `content`, emit `refused`, emit `metadata`
with empty content.  

### A9 — FactChecker retry launders refuse into 200

**Control:** Sticky invariant — refuse short-circuits before
`enforce_chat_synthesis`; leaked text never enters history.  

### A10 — Resolve unwrap regression when detection off

**Control:** `wrap_resolve_query` always XML-wraps; canary flag only toggles
`canary=` kwarg.  

### A11 — Canary on trusted instruction instead of untrusted analyses

**Control:** Round-table injects only into analyses context section.  

### A12 — Flag flood / missing cooldown key

**Control:** `subject_id=surface` + `insert_flag_once` cooldown.  

## Security Acceptance Criteria

1. Default-off byte-identical per surface table in DATA_FLOW.  
2. Detect leak → flag + content delivered.  
3. Detect+enforce leak → refuse, empty content, sticky chat short-circuit.  
4. No token in API JSON or flag detail.  
5. Enforce-without-detect no-op.  
6. Store errors never fail the request; observe I/O off-loop.  
7. Three surfaces only; canary co-located with untrusted fields as pinned.  
8. GOVERNANCE capability + Non-Claim land as drafted.  

## Residual Risks (accepted)

- Substring FN; intermediate paths unscanned; no egress/tool disable on leak
  (separate backlog).
