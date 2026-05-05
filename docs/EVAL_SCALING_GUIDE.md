# Eval Scaling Guide

**Purpose:** How to scale from 20 generic evals to 50+ domain-specific evals

---

## Starting Point: 20 Generic Evals (included)

Your project ships with 20 eval tasks in `evals/tasks/`:

| Category | Count | Tests |
|----------|-------|-------|
| Security | 5 | Prompt injection, hallucination, boundaries, degradation, empty input |
| Quality | 5 | Output format, instructions, evidence, confidence, context |
| Reliability | 5 | Consistency, refusal, timeout, large input, concurrency |
| System | 5 | Cost, efficiency, multi-turn, errors, round table |

These test your **infrastructure** -- they work regardless of your project's domain.

---

## Scaling to 50+: Add Domain-Specific Evals

### Step 1: Identify failure modes (Week 1)

Sources for domain eval tasks:
- Bugs caught during development
- Manual pre-release checks you do by hand
- User-reported issues or complaints
- Edge cases that surprised you
- Known weaknesses in your LLM's output

Write each failure as: **Given [input], the system should [expected behavior], but instead [actual behavior].**

### Step 2: Create eval tasks (Week 2-3)

For each failure mode, create a test in `evals/tasks/`:

```python
class TestMyDomainFeature:
    def test_known_failure_001(self):
        """Given X input, system should produce Y, not Z."""
        input_data = "..."
        result = my_function(input_data)
        assert expected_property in result
```

### Step 3: Graduate to regression (Ongoing)

When a capability eval consistently passes at **95%+ over 10 runs**, promote it:

1. Move from `evals/tasks/` to `evals/regression/`
2. Add `@pytest.mark.regression` marker
3. Regression tests run on every CI push
4. Target: **100% pass rate** on regression suite
5. Any regression failure blocks the PR

```python
# evals/regression/test_voice_matching.py
@pytest.mark.regression
class TestVoiceMatchRegression:
    """Graduated from capability eval on 2026-03-15 (was passing 98% over 20 runs)."""

    def test_known_good_case_001(self):
        # This used to be in evals/tasks/ -- now it's a regression gate
        ...
```

---

## Grader Selection Guide

| Output Type | Grader | When to Use |
|-------------|--------|-------------|
| JSON with known schema | Code-based | Always preferred -- fast, cheap, deterministic |
| Yes/no decision | Code-based | Check the boolean/enum field |
| Numeric within range | Code-based | Assert bounds |
| Free-form text quality | Model-based | Use rubric template with LLM-as-judge |
| Subjective assessment | Model-based | Pair with structured rubric (1-5 scale per dimension) |
| Gold standard calibration | Human | Quarterly review of model-based grader accuracy |

---

## Non-Determinism: pass@k vs pass^k

| Metric | Formula | Use When |
|--------|---------|----------|
| **pass@k** | P(at least 1 success in k trials) | Tool use, one success matters |
| **pass^k** | P(all k trials succeed) | Customer-facing, consistency matters |

At k=1, they're identical. By k=10, they tell opposite stories.

Example: If pass rate is 80% per trial:
- pass@10 = 99.9% (almost certain at least one succeeds)
- pass^10 = 10.7% (unlikely all ten succeed)

For **regression evals**, use pass^k (consistency). For **capability evals**, use pass@k (can it do it at all?).

---

## Extended Thinking Configuration

For the strategy phase (Phase 0) of the round table, extended thinking improves orchestrator planning quality.

### Anthropic (Claude)
```python
response = client.messages.create(
    model="claude-opus-4-6-20260205",
    max_tokens=16000,
    thinking={
        "type": "enabled",
        "budget_tokens": 10000,  # Tokens allocated for thinking
    },
    messages=[{"role": "user", "content": strategy_prompt}],
)
# Access thinking: response.content[0].thinking
# Access response: response.content[1].text
```

### OpenAI (o-series models)
```python
response = client.chat.completions.create(
    model="o3",  # or o4-mini
    messages=[{"role": "user", "content": strategy_prompt}],
    # Extended thinking is automatic for o-series models
)
```

### Google (Gemini)
```python
response = model.generate_content(
    strategy_prompt,
    generation_config={"thinking_config": {"thinking_budget": 10000}},
)
```

Use extended thinking for:
- Round table strategy planning (Phase 0)
- Synthesis of conflicting agent outputs (Phase 3)
- Complex reasoning tasks where accuracy > speed

Do NOT use extended thinking for:
- Simple lookups or formatting
- High-volume repetitive tasks (cost adds up)
- Streaming responses (thinking adds latency)

---

## Initializer/Worker Pattern

For multi-session projects where an agent works across multiple coding sessions:

```
FIRST SESSION (Initializer):
  1. Parse the user's request into a structured task list (JSON)
  2. Create reproducible startup script (init.sh or setup_check.py)
  3. Create progress notes file
  4. Make initial git commit
  5. Run health check to verify environment

EVERY SUBSEQUENT SESSION (Worker):
  1. STARTUP: Read task list, choose highest-priority incomplete task
  2. STARTUP: Read git logs + progress notes to understand recent work
  3. HEALTH CHECK: Run smoke test, fix existing bugs BEFORE new work
  4. WORK: Implement ONE task (not multiple!)
  5. CLEANUP: Git commit with descriptive message
  6. CLEANUP: Update progress notes (what done, what remains)
  7. CLEANUP: Leave code in merge-ready state (no half-implementations)
```

Key rules:
- JSON task list, not Markdown (agents creatively edit Markdown)
- ONE task per session (prevents context exhaustion mid-implementation)
- Health check BEFORE new work (don't build on broken foundations)
- Descriptive commits (the next session reads git log to get up to speed)
