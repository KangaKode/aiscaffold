# Agentic Governance

How the scaffold governs autonomous agent behavior: who approves what, what gets recorded, what data is protected, and where the money stops. This document maps each governance domain to the code that implements it and the tests that prove it -- and, just as deliberately, states what the scaffold does **not** claim to do.

---

## Capability Matrix

| Domain | What it does | Implementation | Tests |
|--------|--------------|----------------|-------|
| Human oversight | Graduated autonomy levels (1 = trusted, 6 = most restricted) map to approval gates, specialist caps, rate multipliers, and conflict auto-escalation; restricted levels force a human check-in before results are acted on | `orchestration/autonomy.py`, `orchestration/round_table_helpers.py` (`apply_approval_gate`), `orchestration/chat_orchestrator.py`, `learning/checkin_manager.py` | `tests/test_governance.py` |
| Audit trail | Metadata-only deliberation timeline (phases, agent counts, durations, outcomes) keyed by correlation id; detail values are structurally limited so prompt/response content can never be stored | `orchestration/deliberation_audit.py`, `api/routes/audit.py`, `learning/store.py` (`audit_events` table) | `tests/test_governance.py` |
| Data protection | PII redaction (emails, SSNs, Luhn-verified card numbers, IPs, phones, names-in-context) with Unicode normalization against homoglyph/invisible-char obfuscation, applied before correction text is persisted | `security/pii.py`, `learning/corrections.py` | `tests/test_governance.py` |
| Cost control | Per-tenant LLM budgets with warn/exhaust thresholds; exhausted tenants are blocked before the provider call; spend persists across restarts when a store is configured | `llm/budget_manager.py`, `llm/client.py`, `api/routes/budgets.py`, `learning/store.py` (`budget_spend` table) | `tests/test_governance.py` |
| Behavioral monitoring | Agent behavioral baselines, vote-lockstep/collusion detection, correction drift, and user activity thresholds; findings persist as integrity flags for human review | `learning/activity.py`, `learning/collusion.py`, `learning/override_detector.py`, `api/routes/activity.py` | `tests/test_learning_store.py` |
| Access control | Agent identity tokens verified at dispatch, per-agent rate limits, scope filtering of task context, suspension and credential lifecycle | `agents/identity.py`, `agents/rate_limiter.py`, `orchestration/scope_filter.py`, `agents/registry.py` | `tests/test_agent_identity.py` |
| Knowledge integrity | Content policy screens knowledge writes for standing-rule manipulation ("always classify X as safe", "don't log this"); rejected writes are refused and recorded as integrity flags; corrections require four-eyes human approval | `learning/content_policy.py`, `learning/corrections.py`, `learning/override_detector.py` | `tests/test_governance.py`, `tests/test_learning_store.py` |

---

## Known Limitations / Non-Claims

Stating what a control does *not* do is part of the control. These are the honest boundaries of the current implementation:

- **Heuristic classifiers are not ML-grade.** The content policy and override detector are regex/keyword heuristics. They catch obvious manipulation phrasings and will miss paraphrases, other languages, and creative rewording. They reduce risk; they do not eliminate it.
- **In-memory rate limits and budgets-without-a-store reset on restart.** Agent rate limits are process-local sliding windows. Budget spend only survives restarts when a learning store is configured; without one, a restart is a budget reset.
- **The audit trail is advisory, not tamper-proof.** Audit events live in the same database as application data, writes are fire-and-forget (a failed write is logged, not raised), and rows can be altered by anyone with database access. For non-repudiation you need append-only storage and timestamp attestation (see PLATFORM_GUIDE.md compliance section).
- **PII patterns are not a compliance guarantee.** Redaction covers common structured formats (emails, SSNs, card numbers, IPs, phones) plus a deliberately modest name heuristic. It will miss unstructured personal data, non-US formats, and context-dependent identifiers. It is a harm-reduction layer, not a GDPR/CCPA control by itself.
- **Single-node assumptions.** Tenant context uses process-local contextvars, autonomy env overrides are parsed once per process, and check-ins/budgets assume one coordinating instance unless you point everything at a shared store. Multi-node deployments need shared persistence and per-instance configuration discipline.

---

## Extension Points

- **SIEM export**: the `audit_events` table is a clean feed for a log shipper -- poll by `created_at` and forward to your SIEM.
- **Ground-truth verification**: plug a real `GroundTruthProvider` into the enforcement pipeline so numeric claims are checked against authoritative data.
- **Multi-provider model diversity**: run safety agents on a different LLM provider than domain agents so one provider's blind spots don't propagate.
- **Workload identity**: replace `issue_token`/`verify_token`/`hash_token` in `agents/identity.py` with your corporate STS or service-mesh identity system.
- **Real-time human escalation**: wire `CheckInManager` to a pager/chat integration so approval-required results notify a person immediately instead of waiting to be polled.
- **Per-call sandboxing**: execute agent-initiated tool calls inside a sandbox (container, seccomp, or WASM) so a manipulated agent's blast radius stays bounded.
