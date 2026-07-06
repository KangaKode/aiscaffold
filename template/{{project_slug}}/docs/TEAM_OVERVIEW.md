# Team Overview

A 5-minute read for stakeholders, managers, and new team members.
No AI expertise required.

---

## What Is This?

This is a **multi-agent AI system** -- a team of specialized AI assistants that collaborate to answer questions and review work. Instead of relying on a single AI, multiple agents with different expertise analyze the same task, challenge each other, and produce a consensus recommendation.

Think of it like a review board: each member brings a different perspective, they debate, and they vote on the final answer.

---

## How Does It Work?

### The Round Table Protocol

1. **Premise Gate** -- Before spending anything, each agent quickly checks whether the task's premise is sound. If enough agents independently refuse, the system declines the task and explains what is wrong and how to reframe it.
2. **Strategy** (optional) -- A planning phase where the system decides how to approach the task.
3. **Independent Analysis** -- Each agent analyzes the task separately, without seeing other agents' work. Agent prompts request evidence-backed findings.
4. **Challenge** -- Agents review each other's analyses and raise objections or concessions. This catches blind spots.
5. **Synthesis + Voting** -- Findings are merged into a recommendation. Each agent votes approve or dissent with reasons.

### Quick Chat Mode

For simpler questions, the **chat orchestrator** selects 1-3 relevant agents, consults them, cross-checks their answers, and responds in seconds -- no full round table needed. If it detects disagreement or complexity, it suggests escalating to the full protocol.

---

## Why This Architecture?

| Design Choice | Reason |
|---------------|--------|
| **Multiple agents** | No single AI is good at everything. Specialists with domain boundaries produce better results than one generalist. |
| **Evidence discipline** | Agent prompts request citations and evidence levels. Validators check malformed tags when present, and stricter evidence requirements can be added for production workflows. |
| **Challenge phase** | Agents check each other. Errors caught by peers before reaching users. |
| **Voting + dissent** | Minority views are preserved, not silenced. Decision-makers see the full picture. |
| **Prompt caching** | System instructions are cached across calls, cutting input-token costs by up to ~90% on cached content (provider-dependent). |
| **Trust scores** | User feedback adjusts which agents get consulted. The system learns who you trust over time. |

---

## It Gets Better With Use

When your team uses the learning features, the platform accumulates institutional knowledge instead of starting from zero every session. When you accept or reject an answer, the responsible agent's trust score moves, and future questions route toward the agents you have found reliable. When an answer was wrong, a reviewer can record the correct one as a **correction** -- a second person must approve it before it takes effect -- and from then on every answer path (quick answers, chat, and full team analyses) is grounded in those approved corrections. Each approval also automatically distills recurring mistakes into reusable "error schemas" and checks the knowledge base for contradictions.

Two guardrails keep this trustworthy: nothing adapts silently (behavior changes go through an explicit check-in you approve or dismiss), and learned knowledge is governed (corrections are policy-screened, four-eyes approved, auditable, and erasable). Knowledge also ages: corrections that nobody has re-confirmed recently are surfaced as stale for review, and a built-in governance report summarizes deliberation outcomes, integrity flags, and knowledge health for any date range.

---

## Safety Properties

- **Input validation** -- User input is size-limited and validated at the API, and untrusted content is screened for injection patterns and wrapped in neutralized delimiters wherever it is composed into an AI prompt.
- **Prompt guard** -- A sanitization layer detects and neutralizes prompt injection attacks.
- **Rate limiting** -- Per-IP request limits prevent abuse; automatic cleanup prevents memory exhaustion.
- **API authentication** -- Bearer token auth on all data endpoints. Required in production; optional in development.
- **SSRF protection** -- Agent registration URLs are validated against private IPs, internal hostnames, and dangerous schemes.
- **Domain boundaries** -- Agents flag when they are asked about topics outside their expertise instead of guessing.

---

## Key Metrics

| Metric | What It Tells You |
|--------|------------------|
| **Consensus rate** | How often agents agree. High = clear answers. Low = genuinely complex topics. |
| **Approval rate** | Percentage of agents voting "approve" on a recommendation. |
| **Trust scores** | Which agents users find most reliable, based on accept/reject feedback. |
| **Token usage** | LLM cost. Lower is better. Prompt caching keeps this efficient. |
| **Task duration** | Time from submission to result. Typically seconds for chat, 10-30s for round table. |

---

## How to Get Started

1. **Try it:** `make demo` runs a mock round table with no API keys needed.
2. **Read the tutorial:** `docs/TUTORIAL.md` walks through creating your first agent in 30 minutes.
3. **Check the glossary:** `docs/GLOSSARY.md` defines all the AI terminology used in this project.
