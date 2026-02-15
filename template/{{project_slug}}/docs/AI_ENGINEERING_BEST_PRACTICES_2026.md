# AI Engineering Best Practices 2026 - Research Synthesis

**Date:** January 2026
**Sources:**
- OpenAI: Harness Engineering
- Anthropic: Effective Harnesses for Long-Running Agents
- Anthropic: Demystifying Evals for AI Agents
- Anthropic: Multi-Agent Research System
- Anthropic: Complete Guide to Building Skills for Claude
- Subagents.cc: Agent Catalog (104 agents reviewed)

**Purpose:** Reference guide for AI engineering best practices. All agents in this project should follow these patterns.

---

## EXECUTIVE SUMMARY

Six major themes emerge across all sources:

1. **Context is a scarce resource** - Stop dumping everything into one file. Use progressive disclosure.
2. **Externalize state aggressively** - Don't rely on context windows. Use files, databases, structured logs.
3. **Grade outcomes, not paths** - Evaluate what the agent produced, not the steps it took.
4. **Encode taste as enforceable invariants** - Human preferences captured once, enforced everywhere.
5. **Multi-agent orchestration works** - 90% improvement over single-agent on research tasks.
6. **Skills are portable expertise** - Teach once, benefit every conversation.

---

## PART 1: HARNESS ARCHITECTURE

### 1.1 What Is a "Harness"?

The harness is the complete agent loop that orchestrates between user, model, and tools. Its components:

| Component | Purpose |
|-----------|---------|
| Core Agent Loop | Orchestration between user, model, and tools |
| Thread Lifecycle | Create, resume, fork, archive conversation threads |
| Config & Auth | Load configuration, manage defaults, run auth flows |
| Tool Execution | Execute tools in sandbox, wire up integrations |

### 1.2 The Item/Turn/Thread Model (OpenAI)

Three primitives for all agent interactions:

- **Item** - Atomic unit of I/O (message, tool call, approval request). Lifecycle: `started` -> `delta` (streaming) -> `completed`
- **Turn** - One unit of agent work initiated by user input. Contains a sequence of Items.
- **Thread** - Durable container for ongoing session. Contains multiple Turns. Can be created, resumed, forked, archived.

### 1.3 The Initializer/Worker Pattern (Anthropic)

For long-running work across sessions:

```
FIRST RUN ONLY:
  Initializer Agent
  ├── Expand prompt -> structured feature list (JSON)
  ├── Create init.sh (reproducible startup)
  ├── Create progress notes file
  └── Make initial git commit

EVERY SUBSEQUENT SESSION:
  Coding Agent
  ├── STARTUP: Read feature list, git logs, progress notes
  ├── HEALTH CHECK: Run smoke test, fix existing bugs first
  ├── WORK: Pick ONE incomplete feature, implement, test
  └── CLEANUP: Git commit, update progress notes, leave clean state
```

**Key insight:** "When something failed, the fix was almost never 'try harder.' It was always 'what capability is missing from the environment?'"

---

## PART 2: CONTEXT MANAGEMENT

### 2.1 The "One Big File" Anti-Pattern

> "A single blob doesn't lend itself to mechanical checks (coverage, freshness, ownership, cross-links), so drift is inevitable. When everything is 'important,' nothing is."

### 2.2 Progressive Disclosure (Three Levels)

| Level | When Loaded | Content | Token Cost |
|-------|------------|---------|------------|
| Level 1 | Always | YAML frontmatter / table of contents (~100 lines) | Minimal |
| Level 2 | When relevant | Full instructions (< 500 lines) | Moderate |
| Level 3 | On demand | Reference files, templates, detailed docs | Only as needed |

### 2.3 Make Everything Repository-Local

> "From the agent's point of view, anything it can't access in-context while running effectively doesn't exist."

All design decisions, architectural patterns, product specs, and engineering norms must live as versioned artifacts in the repository.

---

## PART 3: STATE MANAGEMENT

### 3.1 Three-Layer External State

| Layer | Format | Purpose | Example |
|-------|--------|---------|---------|
| Feature/Task List | JSON | What remains to be done | `features.json` with pass/fail per feature |
| Progress Notes | Text | What was recently attempted | `progress.txt` with session summaries |
| Git History | Commits | Exactly what code changed and why | Descriptive commit messages |

**Why JSON over Markdown for agent-managed state:**
> "The model is less likely to inappropriately change or overwrite JSON files compared to Markdown files."

### 3.2 Session Startup Ritual

Every agent session should:
1. Read the task list and choose highest-priority incomplete task
2. Read git logs and progress files to get up to speed
3. Confirm working directory
4. Run health check (verify nothing is broken)
5. Fix any existing bugs BEFORE starting new work

### 3.3 Session Cleanup Ritual

Every agent session should end with:
1. Git commit with descriptive message
2. Update progress notes
3. Leave code in merge-ready state
4. No half-implemented features

---

## PART 4: MULTI-AGENT ORCHESTRATION

### 4.1 Orchestrator-Worker Pattern

```
User Query
    |
    v
Lead Agent (high-capability model)
├── Analyzes query
├── Develops strategy (extended thinking)
├── Saves plan to external memory
├── Spawns specialized subagents
│   ├── Subagent A (efficient model) ──┐
│   ├── Subagent B (efficient model) ──┤── Parallel execution
│   └── Subagent C (efficient model) ──┘
├── Synthesizes results
├── Decides if more research needed
└── Returns final result
```

**Performance:** Multi-agent outperformed single-agent by **90.2%** on research evaluations.

**Cost reality:** "Agents use ~4x more tokens than chat. Multi-agent uses ~15x more tokens than chat." Justify with task value.

### 4.2 Subagent Design Requirements

Each subagent must receive:
- **Specific objective** (not a vague topic)
- **Output format** specification
- **Tool/source guidance**
- **Clear task boundaries** to prevent overlap

Without detailed task descriptions: "agents duplicate work, leave gaps, or fail to find necessary information."

### 4.3 Communication: Hub-and-Spoke, Not Mesh

- Subagents report to lead agent, not to each other
- Subagents write outputs to **filesystem** to minimize "game of telephone"
- Pass lightweight references back to coordinator, not full content

### 4.4 Token Usage Explains 80% of Performance Variance

Distributing work across agents with **separate context windows** is key to performance.

---

## PART 5: EVALUATION (EVALS)

### 5.1 Types of Evals

| Type | Purpose | Target Pass Rate |
|------|---------|-----------------|
| Capability ("quality") | What can this agent do well? | Start low, improve over time |
| Regression | Does the agent still handle old tasks? | ~100% |
| Graduation | Capability evals with consistently high pass rates become regression suites | Continuous |

### 5.2 Start Here (20-50 Tasks)

> "20-50 simple tasks drawn from real failures is a great start."

Sources for eval tasks:
- Manual pre-release checks you already do
- Bug tracker items
- Support queue issues
- Known failure modes

### 5.3 Three Types of Graders

| Type | Best For | Trade-offs |
|------|----------|------------|
| Code-based | Pass/fail, string match, lint, outcome verification | Fast, cheap, reproducible; brittle to valid variations |
| Model-based | Freeform output, nuance, rubric scoring | Handles nuance; expensive, non-deterministic |
| Human | Gold standard calibration | Slow, expensive; essential for calibration |

### 5.4 Critical Eval Principle

> **"Grade what the agent produced, not the path it took."**

### 5.5 Non-Determinism Metrics

- **pass@k** - Probability of at least one success in k trials (rises with k). Use when one success matters.
- **pass^k** - Probability ALL k trials succeed (falls with k). Use for customer-facing reliability.

---

## PART 6: SKILLS ARCHITECTURE

### 6.1 What Is a Skill?

A skill is a folder teaching Claude how to handle specific tasks:

```
my-skill/
├── SKILL.md         <- Required: Instructions with YAML frontmatter
├── scripts/         <- Optional: Executable code
├── references/      <- Optional: Documentation loaded as needed
└── assets/          <- Optional: Templates, fonts, icons
```

**Analogy:** MCP provides the kitchen (tools). Skills provide the recipes (how to use them).

### 6.2 Progressive Disclosure in Skills

- **YAML frontmatter** (always loaded) - When to use, trigger phrases
- **SKILL.md body** (loaded when relevant) - Full instructions, < 500 lines
- **Referenced files** (loaded on demand) - Detailed docs, templates

### 6.3 Degrees of Freedom

| Freedom Level | When | Example |
|---------------|------|---------|
| High (text instructions) | Multiple valid approaches | Code review, ideation |
| Medium (pseudocode) | Configuration affects behavior | Report generation with templates |
| Low (exact scripts) | Fragile operations | Database migrations, exact matching |

### 6.4 Key Anti-Patterns

- Don't use "one big AGENTS.md" - progressive disclosure instead
- Don't offer too many options - provide defaults with escape hatches
- Don't include time-sensitive information
- Don't deeply nest references (keep 1 level deep from SKILL.md)
- Don't assume the skill is the only loaded capability

---

## PART 7: ARCHITECTURAL PATTERNS

### 7.1 Rigid Layered Architecture with Enforced Dependency Directions

Enforce strict layer ordering. Lower layers NEVER import from higher layers. This is validated by `tests/test_architecture.py`.

Cross-cutting concerns enter through a single explicit interface (Providers).

### 7.2 Parse at the Boundary

> "Parse data shapes at the boundary."

Data entering the system is parsed into typed structures at the boundary, not passed around as raw dictionaries.

### 7.3 Custom Linters with Remediation Instructions

Lint error messages are structured prompts that tell agents *how to fix the issue*:

- Naming conventions for schemas and types
- File size limits
- Dependency direction validation
- Structured logging enforcement

### 7.4 Favor "Boring" Technology

> "Technologies described as 'boring' tend to be easier for agents to model due to composability, API stability, and representation in training data."

### 7.5 Continuous Garbage Collection

> "Technical debt is like a high-interest loan."

- Golden principles encoded in repo
- Recurring background tasks scanning for deviations
- Quality grading over time (QUALITY_SCORE.md per domain)

### 7.6 Agent-to-Agent Review

> "Over time, we've pushed almost all review effort towards being handled agent-to-agent."

Pattern: Agent writes code -> Agent reviews own changes -> Requests additional agent reviews -> Responds to feedback -> Iterates until all reviewers satisfied.

---

## PART 8: SUBAGENTS

### Recommended Agent Roster

| Agent | Role | When to Use |
|-------|------|-------------|
| **solution-architect** | System architecture, design decisions | Before any new feature |
| **codebase-scout** | Check existing code before writing new | Before creating files/functions |
| **data-flow-guardian** | Validate data paths, source of truth | When adding data operations |
| **minimalist** | Prevent over-engineering, AI bloat | When reviewing changes > 100 lines |
| **code-reviewer** | Quality, security, maintainability | After making changes |
| **red-team** | Adversarial pre-commit security gate | Before every commit |
| **security-hardener** | Defensive security, input validation | When adding endpoints/user input |
| **prompt-engineer** | Prompt design, 2026 Skills patterns | When writing LLM prompts |
| **ai-engineer** | Multi-agent architecture, orchestration | When designing agent systems |
| **test-architect** | Test strategy, eval design | When writing tests |
| **debugger** | Root cause analysis | When things break |
| **project-curator** | Directory structure, cleanliness | When reorganizing |
| **sql-pro** | Database optimization | When working with DB |
| **ux-researcher** | User workflow optimization | When designing UI |

---

## KEY QUOTES TO REMEMBER

> "Give the agent a map, not a 1,000-page manual." -- OpenAI

> "Compaction isn't sufficient. The agent still fails." -- Anthropic

> "Grade what the agent produced, not the path it took." -- Anthropic

> "Agent failure is a signal about the environment, not about the agent." -- OpenAI

> "Human taste is captured once, then enforced continuously on every line of code." -- OpenAI

> "The context window is a public good. Your skill shares it with everything else." -- Anthropic

> "Token usage explains 80% of performance variance." -- Anthropic

> "20-50 simple tasks drawn from real failures is a great start." -- Anthropic

---

*This document is included in every project scaffolded by aiscaffold. All agents reference it.*
