# Architecture Map: Runtime Canary Enforcement

**Status:** Design artifact (High risk-tier) — REV 2 (expert blockers folded)  
**Risk-tier rationale:** Touches `security/**` primitives and response-path
opt-in enforcement (refusal). Highest applicable tier wins.

## Existing Components

| Component | Role today |
|-----------|------------|
| `security/injection_defense.py` (`inject_canary`, `check_canary`) | Mint `[SEC_CANARY_<hex>]`; exact-substring leak detect |
| `security/prompt_guard.py` (`wrap_user_content(..., canary=True)`) | Optional canary inside XML wrap; default `canary=False` |
| `api/detection_hooks.py` (`run_startup_canary_check`) | Opt-in startup self-test only; never scans live responses |
| `orchestration/chat_orchestrator.py` (`_synthesize`) | Final chat LLM call; raw `user_message` (no wrap) today |
| `orchestration/single_shot.py` | Always `wrap_user_content(query, label="TASK_CONTENT")` without canary |
| `orchestration/round_table_helpers.py` (`phase_synthesis`) | Trusted fixed `user_message` instruction; untrusted `analyses_json` in `context` |
| Sentinel refusal | Precedent: `sentinel_refusal` on result → API `status/refusal_source/refusal_reason` |
| `learning/flags.py` | Fire-and-forget integrity flag persistence |

## New Module: `orchestration/runtime_canary.py` (cap 200 lines)

Security stays Layer-0. Helper owns env parse, per-surface wrap adapters,
observe/flag, refuse decision.

| API | Behavior |
|-----|----------|
| `detection_enabled()` | `RUNTIME_CANARY_ENABLED` in `true`/`1`/`yes` |
| `enforcement_enabled()` | Detection on **and** `RUNTIME_CANARY_ENFORCEMENT_ENABLED` truthy |
| `wrap_chat_user(content, label="USER_CONTENT")` | **Detection off:** `(content, None)` byte-identical (still unwrapped). **On:** `wrap_user_content(..., canary=True)` |
| `wrap_resolve_query(content, label="TASK_CONTENT")` | **Always** XML-wraps (preserves today's resolve boundary). **Detection off:** `wrap_user_content(..., canary=False)` → `(wrapped, None)`. **On:** `canary=True` → `(wrapped, token)` |
| `canary_context_section(content, label="SYNTHESIS_ANALYSES")` | For round-table **untrusted analyses blob only**. **Off:** `(content, None)`. **On:** `inject_canary(content, label)` (no XML wrap change to the trusted instruction `user_message`) |
| `observe_response(text, token, *, store, tenant_id, surface) -> bool` | If no token: false. Else `check_canary`; on hit schedule flag write (see Data Flow); return leaked |
| `should_refuse(leaked) -> bool` | `leaked and enforcement_enabled()` |

**Forbidden:** a single passthrough helper that replaces resolve's existing
`wrap_user_content` when detection is off (that would silently unwrap).

Per-call token in local variables only — no process-global canary state.

### Flag contract (pinned)

- `flag_type`: `canary_leak`
- `subject_id`: **exactly the surface string** (`chat_synthesis` |
  `resolve` | `round_table_synthesis`) — cooldown per surface+tenant
- `detail_json` allowlist only: `{"surface": "<same>"}` — **never** token,
  never response body, never prompt

### Observe I/O (pinned)

From async orchestration: persist via `asyncio.to_thread(insert_flag_once, ...)`
(or equivalent off-loop). Never block the event loop on store I/O. Failures
logged; never raised.

## Wire Sites (frozen v1 — three only)

1. **Chat `_synthesize`:** `wrap_chat_user(message)`; observe immediately after
   each `llm.call` **before returning**. See sticky-refuse invariant below.
2. **Resolve:** replace bare wrap with `wrap_resolve_query(query)`; observe after
   `llm.call` before FactChecker / confidence gate.
3. **Round-table `phase_synthesis`:** `canary_context_section(analyses_json)` into
   `context`; leave trusted `user_message` instruction unchanged; observe on
   `response.content` before parse/return.

### Sticky canary-refuse invariant (chat) — MUST

1. `_synthesize` observes before every return of model text.
2. If `should_refuse(leaked)`: return a structured refuse signal (not leaked
   text). `chat()` MUST short-circuit — **do not** enter
   `enforce_chat_synthesis` / FactChecker retry on a refused synthesis.
3. Leaked text MUST NOT be appended to conversation history.
4. Detect-only leak: flag, return content as today (including FactChecker path);
   each re-synthesize attempt gets its own token and observe.

### Round-table result field (pinned name)

`RoundTableResult.canary_refusal: CanaryRefusal | None` parallel to
`sentinel_refusal`.

```python
@dataclass
class CanaryRefusal:
    reason: str = "canary_leak"  # bounded enum; only value in v1
```

On enforce leak after synthesis: set `canary_refusal`, skip voting (mirror
`finalize_sentinel_refusal` ordering).

## API Surface (pinned)

| Surface | Fields |
|---------|--------|
| Chat / Resolve JSON | `refused: bool`, `refusal_source: str \| None` (`"canary"` when refused), `refusal_reason: str \| None` (`"canary_leak"`) |
| Chat SSE on refuse | **Skip** `event: content` with model text. **Emit** `event: refused` with `{"refusal_source":"canary","refusal_reason":"canary_leak"}`. **Then** emit `event: metadata` including the same refuse fields + empty content. |
| Round-table | Existing `status="refused"`, `refusal_source="canary"`, `refusal_reason="canary_leak"` (no parallel `refused` bool — multi-gate vocabulary already uses status) |

### Resolve: `refused` vs `escalated`

Distinct outcomes. Canary refuse: `refused=true`, `escalated=false`,
`content=""`. Escalate remains confidence/coverage fallback to chat. Docs:
do not auto-retry a canary-refused query as escalate without operator review.

## Unchanged

- `STARTUP_CANARY_ENABLED` independent; `detection_hooks.py` stays boot-only
- No schema/migration; no metrics claimed in v1
- Intermediate agent / cross-check / premise / MCP bodies out of scope

## Implementation Order

1. Artifacts APPROVED (this REV 2)
2. Tests first (`test_runtime_canary.py.jinja`, line-cap header)
3. Helper + three wires + API/SSE
4. Docs (GOVERNANCE draft in WORKFLOW_STATES; env/OPERATIONS/PLATFORM_GUIDE/SECURITY_MODEL)
5. `validate_generated.sh`
