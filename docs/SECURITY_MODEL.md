# Security Model

How the scaffold thinks about threats, where the controls live, and what is honestly out of scope. For vulnerability reporting, see [SECURITY.md](../SECURITY.md).

The single source of truth for the control-by-control capability matrix (implementation files and the tests that prove each control) and the stated non-claims is the generated project's own governance document: [GOVERNANCE.md](../template/%7B%7Bproject_slug%7D%7D/docs/GOVERNANCE.md). It ships inside every generated project, so the people operating the system always have it next to the code. This page is the map; that document is the territory.

---

## Defense in Depth: One Request's Journey

Every control below is implemented and tested in the generated project. The diagram shows where each control sits relative to one request. Two honest notes on wiring: injection layers 1-2 are applied at each surface where untrusted content is composed into a prompt (user messages, remote agent responses, MCP tool output) rather than as one ingress filter, and output signing is a primitive you invoke on results you emit downstream, not an automatic step.

```mermaid
flowchart TD
    Req[Incoming request] --> Auth["API-key auth + per-IP rate limiting"]
    Auth --> Tenant["Tenant context via AuthContext (tenant_id on every route)"]
    Tenant --> Inj["Injection defense at every untrusted-content surface:\nLayer 1 static pattern guard -> Layer 2 Unicode/encoding normalization -> delimiter wrapping with fence-break neutralization"]
    Inj --> Dispatch["Agent dispatch gates: JWT identity verified, per-agent rate limits, capability scopes filter context"]
    Dispatch --> Delib["Multi-agent deliberation"]
    Delib --> L3["Layer 3 inside the deliberation: Sentinel semantic screen - fails closed without an LLM"]
    L3 --> Enforce["Evidence enforcement: unsupported-confidence claims rejected"]
    Enforce --> Gate["Autonomy policy / human approval gate"]
    Gate --> Audit["Metadata-only audit trail + reasoning-chain hash"]
    Audit -.-> Sign["Output signing (HMAC attestation primitive - invoke on results you forward)"]
```

No single layer is trusted to be perfect; each assumes the one before it can be bypassed.

## Prompt Injection: The Three Layers, and Where Sentinel Sits

Zooming into the injection-defense portion of that path -- each layer catches what the previous one cannot, and the Sentinel agent guards both directions:

```mermaid
flowchart TD
    Input["Untrusted content (user input, agent responses, MCP tool output)"] --> L1["Layer 1 - Static pattern guard (security/prompt_guard.py)"]
    L1 -->|"catches known jailbreaks: 'ignore previous instructions', role-override phrasings, system-prompt probes"| L2["Layer 2 - Normalization (security/injection_defense.py)"]
    L2 -->|"catches evasion: Cyrillic homoglyphs, zero-width chars, base64/rot13-encoded payloads"| Wrap["Boundary wrapping (wrap_user_content): canary token + fence-break tag neutralization"]
    Wrap --> L3["Layer 3 - Sentinel semantic screen (agents/core/sentinel.py)"]
    L3 -->|"catches meaning: social engineering, methodology extraction, context poisoning, privilege probing"| Delib["Deliberation proceeds"]
    L3 -.->|"no LLM available"| Closed["FAILS CLOSED: Sentinel refuses (sentinel_unavailable) instead of passing unscreened input"]
    Delib --> OutGate["Sentinel OUTPUT gate (challenge phase): screens peer analyses for system-prompt leakage, architecture disclosure, methodology exposure"]
    OutGate --> Vote["Sentinel casts a dissent vote when synthesis leaks internals - dissent is preserved in the result"]
```

Layers 1 and 2 are deterministic (regex + Unicode analysis, no LLM, free); the [README shows them catching real payloads](../README.md#see-it-run). Layer 3 is the only layer that understands intent, which is why it is an agent inside the deliberation rather than a filter in front of it: Sentinel screens the input as its Phase 1 analysis, screens the other agents' outputs for leaks as its Phase 2 challenge, and casts a dissent vote at synthesis that is preserved in the result rather than silenced. The adversarial harness attacks all three layers in CI.

## Trust Boundaries

Generated projects treat the following as **untrusted** at all times:

| Boundary | Attack surface | Primary controls |
|----------|----------------|------------------|
| User input (chat, tasks, API bodies) | Prompt injection, encoding attacks, oversized payloads | 3-layer injection defense (static patterns, Unicode/encoding normalization, semantic Sentinel screening), input size limits, input validation on every mutating route |
| Remote agent registration | SSRF, private-IP and metadata-endpoint access, credential exfiltration | URL validation (scheme, private ranges, cloud metadata IPs), API keys resolved from environment only, identity token hashes only on disk |
| Remote agent responses | Injection via analysis/challenge/vote fields, oversized responses | Response sanitization, size limits, adversarial harness coverage |
| MCP tool output | Injection via tool results | Sanitization + `MCP_DATA` boundary wrapping, per-tenant registry, `mcp:` scope gating, non-fatal failure handling |
| Persisted state (registry, learning store) | Tampering with visibility/tenant metadata, poisoned corrections | Validation on load (invalid entries skipped, never widened), four-eyes correction approval, content policy screening, override/collusion/contradiction detectors |
| The agents themselves | Misbehaving or compromised agents | Per-agent JWT identity verified at dispatch, per-agent rate limits, scope-filtered task context, suspension lifecycle, behavioral baselines and lockstep detection |

## The Agents Are Inside the Perimeter

The last row of that table deserves its own section, because it is the scaffold's least common design decision. Most agent frameworks treat agents as trusted extensions of the application. This scaffold treats them as insiders, the way a mature security program treats people with badges: authenticated, least-privileged, behaviorally monitored, and removable -- because an agent holds credentials, touches data, and acts at machine speed, and any of them can be compromised through the content they read.

| Control | What it does | Where it lives |
|---------|--------------|----------------|
| Identity | Per-agent JWT verified at dispatch; tokens are SHA-256 hashed and only the hash is persisted -- the raw token is issued once and held in memory, and a reloaded agent must rotate credentials before it passes verification | `agents/identity.py`, `agents/registry_persistence.py` |
| Least privilege | Capability scopes filter what each agent sees; out-of-scope findings are flagged | `agents/capability.py`, `orchestration/scope_filter.py` |
| Behavioral baselines | Refusal rate, confidence, latency, and scope discipline compared against each agent's own history -- valid credentials with anomalous behavior still get flagged | `learning/activity.py` |
| Multi-step patterns | Sequences of individually-benign actions that add up to extraction are detected across a window | `harness/sequence_detector.py` |
| Collusion | Lockstep agreement between agents that should be independent is flagged (e.g. two agents defeating four-eyes review) | `learning/collusion.py` |
| Containment | Suspension removes an agent from dispatch and listings without deleting its record | `agents/registry.py` |
| Accountability | Reasoning-chain hashes make after-the-fact edits detectable; corrections that shape future behavior need two humans | `security/reasoning_chain_hash.py`, `learning/corrections.py` |

The lifecycle, end to end:

```mermaid
flowchart LR
    Reg["Agent registers"] --> Val["URL validated: SSRF checks block private IPs and metadata endpoints"]
    Val --> Tok["JWT issued once - hash stored, raw token never persisted"]
    Tok --> Disp["Scoped dispatch: identity verified, context filtered to granted scopes"]
    Disp --> Mon["Monitoring: baselines, sequence detection, collusion detection"]
    Mon -->|"anomaly"| Flag["Flagged for human review"]
    Flag -->|"confirmed"| Susp["Suspended: excluded from dispatch, record retained"]
    Mon --> AuditT["Tamper-evident audit trail"]
```

## Posture Decisions

- **Fail closed.** When a control cannot run -- Sentinel has no LLM, persisted metadata fails validation, an identity token is missing -- the system dissents, skips, or blocks. It never silently passes.
- **Secrets never persist in the clear.** Raw identity tokens live in memory only; disk gets SHA-256 hashes. Agent API keys come from environment variables, never from persistence files.
- **Audit without content.** The deliberation audit trail is structurally metadata-only (phases, counts, durations, outcomes) so prompt and response content cannot leak through it. Each run stores a reasoning-chain hash making after-the-fact edits detectable.
- **Human gates on state that shapes behavior.** Corrections require four-eyes approval; integrity findings are flagged for human review, never auto-actioned; restricted autonomy levels force check-ins before results are acted on.

## What Is Out of Scope (Non-Claims)

Stating what a control does *not* do is part of the control. The authoritative list lives in [GOVERNANCE.md -- Known Limitations / Non-Claims](../template/%7B%7Bproject_slug%7D%7D/docs/GOVERNANCE.md#known-limitations--non-claims). Highlights:

- Heuristic classifiers (content policy, override detection) are regex/keyword based -- they reduce risk, they do not eliminate it.
- The audit trail is tamper-evident within a run, not tamper-proof; output signing is symmetric HMAC, not third-party non-repudiation.
- PII redaction is a harm-reduction layer, not a GDPR/CCPA compliance guarantee.
- Erasure removes live rows, not backups or downstream exports.
- Single-node assumptions apply unless persistence is shared and per-instance configuration is disciplined.

## Verification

The scaffold validates its own security posture on every change:

- **Adversarial harness**: six deterministic hostile agents (no LLM, zero cost) attack a full round table with injection payloads in every field; CI asserts containment end-to-end.
- **Red-team scans, Bandit, and AI security checks** run in the 16-check validation pipeline against every generated configuration.
- **588 generated tests** (83% coverage) include dedicated suites for injection defense, agent identity, tamper evidence, governance, and the corrections lifecycle.

The process has caught real issues before release -- for example, a tenant-isolation bug where remote agents reverted to public visibility on restart ([fix](https://github.com/KangaKode/roundtable/commit/9168334ac050e22022ab2787b7b3ff3ce06796cc)).
