# Security Mapping — OWASP LLM & Agentic Top 10

How this scaffold's shipped controls map to the OWASP **Top 10 for LLM
Applications (2025)** and the OWASP **Agentic Security / Agentic AI Top 10
(2026)**. This is an honest coverage map, not a certification: entries state
what the scaffold *does*, what is *opt-in*, and what it explicitly does **not**
claim. Every "does not claim" here is the counterpart of a Non-Claim in
`GOVERNANCE.md` — read the two together.

Rows tagged **(requires `include_evals`)** reference artifacts under `evals/`,
which is only generated when the project opts into evals; the `tests/` and
`src/` citations ship in every profile.

Category names and numbering follow the published OWASP lists as of 2026. OWASP
revises these lists; re-check the identifiers when you cite this map externally.

---

## OWASP Top 10 for LLM Applications (2025)

| ID | Category | Scaffold posture | Where |
|----|----------|------------------|-------|
| LLM01 | Prompt Injection | **Partial, layered, honest about gaps.** Layer 1 static patterns + Layer 2 normalization/decoding scan ingestion boundaries (detect-only on user input; refuse only on knowledge writes); Layer 3 Sentinel semantic screen covers the deliberation path (needs an LLM). Deterministic layers are English-centric; multilingual efficacy is UNMEASURED. Regression-guarded by the golden set; exercised by the hand-crafted + open adversarial corpora. | `security/prompt_guard.py`, `security/injection_defense.py`, `orchestration/ingest_scan.py`, `tests/adversarial_payloads.py`, `tests/adversarial_payloads_open.py`; **(requires `include_evals`)** `evals/tasks/test_injection_defense_golden.py`, `evals/redteam/` |
| LLM02 | Sensitive Information Disclosure | PII redaction with Unicode normalization before correction text is persisted; whitelist-only audit/governance output (never prompt/response content). Pattern-based redaction is harm-reduction, not a compliance guarantee. | `security/pii.py`, `learning/corrections.py`, `orchestration/deliberation_audit.py` |
| LLM03 | Supply Chain | Vendored red-team data carries pinned provenance (immutable upstream commit SHA + per-seed SHA-256 over exact code points) and CC-BY-4.0 attribution; a test recomputes every digest offline. Scaffold does not vet your model/provider supply chain. | `tests/fixtures/provenance.json`, `tests/fixtures/ATTRIBUTION.md`, `tests/test_adversarial_open_corpus.py` |
| LLM04 | Data & Model Poisoning | Learned-knowledge writes require human approval with an explicit four-eyes posture; content-policy screen rejects standing-rule manipulation; contradiction + override detection; trust-loop hardening (opt-in) flags feedback bursts and single-source domination without mutating scores. Four-eyes is not enforceable under a single API key. | `learning/content_policy.py`, `learning/four_eyes.py`, `learning/trust_guard.py`, `learning/override_detector.py` |
| LLM05 | Improper Output Handling | Synthesized output can be HMAC-signed and verified (integrity/attestation for key-sharing consumers, not third-party non-repudiation); remote-agent/MCP output is sanitized + full Layer 1-2 scanned (log-only). | `enforcement/signer.py`, `orchestration/mcp_enrichment.py`, `agents/remote.py` |
| LLM06 | Excessive Agency | Graduated autonomy levels gate approvals, specialist caps, and rate multipliers; restricted levels force a human check-in before results are acted on; Sentinel refusal enforcement (opt-in) can halt a run. | `orchestration/autonomy.py`, `learning/checkin_manager.py`, `orchestration/round_table_helpers.py` |
| LLM07 | System Prompt Leakage | Exfiltration-style payloads are in the Layer 1 pattern set and the adversarial corpora; boundary wrapping with fence-break neutralization on the resolve/premise surfaces. Not all prompt surfaces are wrapped (see GOVERNANCE Non-Claims). | `security/prompt_guard.py`, `orchestration/ingest_scan.py`, `tests/adversarial_payloads.py` |
| LLM08 | Vector & Embedding Weaknesses | **Not applicable as shipped** — the scaffold's knowledge grounding is approved-correction / error-schema text injected as wrapped untrusted content, not a vector store. If you add RAG, this row is yours to fill. | `learning/knowledge_context.py` (non-vector grounding) |
| LLM09 | Misinformation | Round-table deliberation with premise refusal, dissent, and adversarial verification reduces single-model error; enforcement pipeline can check numeric claims against a `GroundTruthProvider` you supply. Consensus is not truth. | `orchestration/premise.py`, `orchestration/round_table.py`, `enforcement/` |
| LLM10 | Unbounded Consumption | Per-tenant LLM budgets with warn/exhaust thresholds, persisted across restarts with a store; agent rate limits; opt-in context-pressure signal warns as prompts near the model window (detect-only, never trims). In-memory limits reset on restart without a store. | `llm/budget_manager.py`, `agents/rate_limiter.py`, `llm/context_pressure.py` |

---

## OWASP Agentic AI Top 10 (2026)

Applied to this scaffold's hub-and-spoke orchestration (an orchestrator
dispatches phases; there is no direct agent-to-agent calling).

| ID | Category | Scaffold posture | Where |
|----|----------|------------------|-------|
| ASI01 | Agent Authorization & Control Hijacking | Agent identity tokens verified before every phase dispatch; per-agent rate limits and scope filtering; suspension/credential lifecycle; identity gate fails closed on ambiguous resolution. | `agents/identity.py`, `orchestration/dispatch_helpers.py`, `orchestration/scope_filter.py` |
| ASI02 | Tool Misuse / Untrusted Tool Output | MCP tool output and remote-agent responses are sanitized and full Layer 1-2 scanned (log-only), so untrusted tool text cannot silently steer a downstream prompt undetected. | `orchestration/mcp_enrichment.py`, `agents/remote.py`, `security/injection_defense.py` |
| ASI03 | Privilege / Identity Compromise | Control-plane tenant isolation: registry keyed by (tenant_id, name), cross-tenant access reads as 404, dispatch gates resolve by object identity. Real tenant/user resolution is your IdP integration (single-key default = one identity). | `agents/registry.py`, `api/routes/agents.py`, `api/middleware/auth.py` |
| ASI04 | Resource & Service Exhaustion | Per-tenant budgets, rate limits, extraction-volume guard (opt-in enforcement), context-pressure signal. | `llm/budget_manager.py`, `learning/extraction_guard.py`, `llm/context_pressure.py` |
| ASI05 | Cascading / Multi-Agent Failures | Fail-closed safety agents (Sentinel refuses, voters dissent on LLM errors); consensus computed over votes actually cast; collusion/lockstep detection (opt-in). | `llm/response_guard.py`, `orchestration/round_table.py`, `learning/collusion.py` |
| ASI06 | Deception & Trust Manipulation | Trust-loop hardening (opt-in): read-time decay, min-interaction gate, burst + single-source-domination detection — flags only, routing never altered, scores never mutated. Session id is caller-supplied (see Non-Claims). | `learning/trust_guard.py`, `api/routes/feedback.py` |
| ASI07 | Memory / Knowledge Poisoning | Human-approved corrections, content-policy screen, contradiction detection, knowledge aging/staleness signal, GDPR erasure. `analyze_correction_drift` (slow poisoning) ships as a library with no runtime call site. | `learning/corrections.py`, `learning/content_policy.py`, `learning/contradiction.py`, `learning/aging.py` |
| ASI08 | Insufficient Observability / Auditability | Metadata-only deliberation audit trail with per-run reasoning-chain SHA-256; bounded Prometheus metrics; optional OpenTelemetry phase spans (exports nothing until you configure a provider). Tamper-evident within a run, not tamper-proof. | `orchestration/deliberation_audit.py`, `security/reasoning_chain_hash.py`, `observability/metrics.py`, `observability/tracing.py` |
| ASI09 | Unsafe Autonomy / Human Oversight Gaps | Graduated autonomy with forced human check-ins at restricted levels; premise refusal short-circuit; Sentinel enforcement (opt-in) halts after Phase 1. | `orchestration/autonomy.py`, `learning/checkin_manager.py`, `orchestration/premise.py` |
| ASI10 | Extraction & Exfiltration | Extraction guard (per-user + tenant-wide volume), timing-regularity + behavioral monitoring (opt-in), backward-chaining sequence detector for low-and-slow playbooks, approval-pair escalation. Several are detect-only and evadable (see Non-Claims). | `learning/extraction_guard.py`, `learning/timing_analysis.py`, `harness/sequence_detector.py`, `learning/approval_patterns.py` |

---

## What this map does NOT claim

- **No benchmark score.** Coverage here means "a control exists and is tested for
  regression," not "attacks in this category are stopped at rate X." The golden
  set is a regression guard; the red-team config is a starter you must wire to a
  provider and tune.
- **No multilingual guarantee.** See `GOVERNANCE.md` — Layers 1-2 are
  English-centric and Layer 3's multilingual efficacy is unmeasured.
- **No Layer 3 coverage in CI.** The semantic screen needs an LLM; deterministic
  CI does not exercise it. LLM-backed evals are your integration.
- **Numbering drifts.** OWASP revises these lists; verify identifiers against the
  current published versions before citing externally.
