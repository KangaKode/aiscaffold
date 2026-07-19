# Security Mapping — OWASP LLM & Agentic Top 10

How this scaffold's shipped controls map to the OWASP **Top 10 for LLM
Applications (2025)** and the OWASP **Top 10 for Agentic Applications
(2026)**. This is an honest coverage map, not a certification: entries state
what the scaffold *does*, what is *opt-in*, and what it explicitly does **not**
claim. Every "does not claim" here is the counterpart of a Non-Claim in
`GOVERNANCE.md` — read the two together.

Rows tagged **(requires `include_evals`)** reference artifacts under `evals/`,
which is only generated when the project opts into evals; the `tests/` and
`src/` citations ship in every profile.

Category names and numbering follow the published lists: OWASP Top 10 for LLM
Applications v2025 (LLM01-LLM10) and OWASP Top 10 for Agentic Applications 2026
(ASI01-ASI10, released 2025-12-09). OWASP revises these lists; re-check the
identifiers when you cite this map externally.

---

## OWASP Top 10 for LLM Applications (2025)

| ID | Category | Scaffold posture | Where |
|----|----------|------------------|-------|
| LLM01 | Prompt Injection | **Partial, layered, honest about gaps.** Layer 1 static patterns + Layer 2 normalization/decoding scan ingestion boundaries (detect-only on user input; refuse only on knowledge writes); Layer 3 Sentinel semantic screen covers the deliberation path (needs an LLM). Deterministic layers are English-centric; multilingual efficacy is UNMEASURED. Regression-guarded by the golden set; exercised by the hand-crafted + open adversarial corpora. Deterministic layers are also measured against pinned subsets (N=60+60+30) of InjecAgent and AgentDojo plus open-corpus continuity **(requires `include_evals`)** — category FN/FP with denominators, not a benchmark score. | `security/prompt_guard.py`, `security/injection_defense.py`, `orchestration/ingest_scan.py`, `tests/adversarial_payloads.py`, `tests/adversarial_payloads_open.py`; **(requires `include_evals`)** `evals/tasks/test_injection_defense_golden.py`, `evals/tasks/test_public_corpus_harness.py`, `evals/redteam/` |
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

## OWASP Top 10 for Agentic Applications (2026)

Applied to this scaffold's hub-and-spoke orchestration (an orchestrator
dispatches phases; there is no direct agent-to-agent calling). Two categories
are deliberately claimed as gaps, not mitigations: ASI05 (not covered) and
ASI07 (not applicable by architecture).

| ID | Category | Scaffold posture | Where |
|----|----------|------------------|-------|
| ASI01 | Agent Goal Hijack | Layered injection defense at ingestion boundaries (detect-only on user input; refuse on knowledge writes); boundary wrapping with fence-break neutralization on resolve/premise; premise refusal short-circuit; Sentinel semantic screen on deliberation, with opt-in enforcement that can halt a run. Per-surface gaps are stated Non-Claims. | `security/prompt_guard.py`, `security/injection_defense.py`, `orchestration/premise.py`, `orchestration/round_table_helpers.py` |
| ASI02 | Tool Misuse and Exploitation | MCP tool output and remote-agent responses are sanitized and full Layer 1-2 scanned (log-only), so untrusted tool text cannot silently steer a downstream prompt undetected; per-agent rate limits and scope filtering bound what a misused dispatch can reach. | `orchestration/mcp_enrichment.py`, `agents/remote.py`, `agents/rate_limiter.py`, `orchestration/scope_filter.py` |
| ASI03 | Identity and Privilege Abuse | Agent identity tokens verified before every phase dispatch, with suspension/credential lifecycle and a fail-closed gate on ambiguous resolution; control-plane tenant isolation (registry keyed by (tenant_id, name), cross-tenant access reads as 404). Real tenant/user resolution is your IdP integration (single-key default = one identity). | `agents/identity.py`, `orchestration/dispatch_helpers.py`, `agents/registry.py`, `api/middleware/auth.py` |
| ASI04 | Agentic Supply Chain Vulnerabilities | **Partial.** Vendored red-team data carries pinned provenance (immutable upstream commit SHA + per-seed SHA-256 over exact code points), recomputed offline by a shipped test. The scaffold does NOT vet your models, MCP servers, or remote agents at runtime — their *output* is scanned (ASI02), but their provenance and update channel are yours to secure. | `tests/fixtures/provenance.json`, `tests/test_adversarial_open_corpus.py` |
| ASI05 | Unexpected Code Execution | **Not covered.** The scaffold neither executes agent-generated code nor ships an execution sandbox; sandboxing is an Extension Point you own (see `GOVERNANCE.md`). No mitigation is claimed for this category. | `GOVERNANCE.md` (Extension Points) |
| ASI06 | Memory and Context Poisoning | Human-approved corrections with an explicit four-eyes posture; content-policy screen; contradiction + override detection; knowledge aging/staleness signal; loop-integrity detection (opt-in) wires the correction-drift and multi-turn poisoning scans, detect-only; GDPR erasure. | `learning/corrections.py`, `learning/content_policy.py`, `learning/contradiction.py`, `learning/loop_integrity.py` |
| ASI07 | Insecure Inter-Agent Communication | **Not applicable by architecture.** Hub-and-spoke: the orchestrator dispatches phases and there is no direct agent-to-agent channel to secure. Remote-agent responses back to the hub are sanitized + scanned (ASI02). This is claimed as N/A, never as "mitigated" — the surface does not exist here; if you add agent-to-agent dispatch, this row becomes yours. | `orchestration/` (hub-and-spoke), `agents/remote.py` |
| ASI08 | Cascading Failures | Fail-closed safety agents (Sentinel refuses, voters dissent on LLM errors); consensus computed only over votes actually cast; collusion/lockstep detection (opt-in); per-tenant budgets and rate limits bound the blast radius of a runaway loop. | `llm/response_guard.py`, `orchestration/round_table.py`, `learning/collusion.py`, `llm/budget_manager.py` |
| ASI09 | Human-Agent Trust Exploitation | Graduated autonomy forces human check-ins at restricted levels; four-eyes approval posture on knowledge writes; metadata-only deliberation audit with per-run reasoning-chain SHA-256 for post-hoc scrutiny; trust-loop hardening (opt-in) flags feedback-loop manipulation without mutating scores. Consensus is not truth. | `orchestration/autonomy.py`, `learning/four_eyes.py`, `orchestration/deliberation_audit.py`, `learning/trust_guard.py` |
| ASI10 | Rogue Agents | **Detect-only signals, not containment.** Behavioral deviation check (operator-invoked), timing-regularity + behavioral monitoring (opt-in), backward-chaining sequence detector for low-and-slow playbooks, extraction-volume guard; agent suspension is a manual control, not an automatic response. Several detectors are evadable (see Non-Claims). | `learning/activity.py`, `learning/timing_analysis.py`, `harness/sequence_detector.py`, `learning/extraction_guard.py` |

---

## What this map does NOT claim

- **No benchmark score.** Coverage here means "a control exists and is tested for
  regression," not "attacks in this category are stopped at rate X." The golden
  set is a regression guard; the public-corpus harness measures pinned subsets
  only (not an AgentDojo / InjecAgent end-to-end score); the red-team config is
  a starter you must wire to a provider and tune.
- **No multilingual guarantee.** See `GOVERNANCE.md` — Layers 1-2 are
  English-centric and Layer 3's multilingual efficacy is unmeasured.
- **No Layer 3 coverage in CI.** The semantic screen needs an LLM; deterministic
  CI does not exercise it. LLM-backed evals are your integration.
- **Numbering drifts.** OWASP revises these lists; verify identifiers against the
  current published versions before citing externally.
