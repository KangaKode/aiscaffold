# Evaluation Guide

How to write evals for your AI agents. Based on [Anthropic: Demystifying Evals for AI Agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents).

---

## Core Principle

> **"Grade what the agent produced, not the path it took."**

Don't check that agents followed specific steps. Check that the output meets quality criteria.

---

## Three Types of Graders

| Type | Best For | Trade-offs |
|------|----------|------------|
| **Code-based** | Pass/fail, schema validation, threshold checks | Fast, cheap, reproducible; brittle to valid variations |
| **Model-based** | Freeform output, nuance, rubric scoring | Handles nuance; expensive, non-deterministic |
| **Human** | Gold standard calibration, edge cases | Slow, expensive; essential for calibration |

### Code-Based Grader

```python
from evals.graders import CodeGrader

grader = CodeGrader("round_table_consensus")
grader.add_check("has_analyses", lambda r: len(r.analyses) > 0)
grader.add_check("consensus_reached", lambda r: r.consensus_reached)
grader.add_check("has_synthesis", lambda r: r.synthesis is not None)
result = grader.grade(round_table_result)
# result.passed, result.checks_passed, result.failures
```

### Model-Based Grader

```python
from evals.graders import ModelGraderConfig, grade_with_model

config = ModelGraderConfig(
    eval_name="synthesis_quality",
    rubric="Is the recommendation specific, actionable, and supported by evidence?",
    pass_threshold=0.7,
)
result = await grade_with_model(llm_client, config, input_text, output_text)
# result.passed, result.score, result.reasoning
```

### Human Grader

```python
from evals.graders import HumanGrader

grader = HumanGrader("edge_case_review")
filepath = grader.submit_for_review(
    input_text="Ambiguous query about auth",
    output_text=agent_response,
    rubric="Did the agent correctly identify the ambiguity and ask for clarification?",
)
# Human reviews evals/human_review/edge_case_review_*.json
# Mark "passed": true/false, add "reviewer" and "notes"
```

---

## Getting Started: Your First 20 Evals

> "20-50 simple tasks drawn from real failures is a great start."

Sources for eval tasks:
1. **Bugs you've already fixed** -- turn each into a regression eval
2. **Manual checks you do before release** -- automate them
3. **Known failure modes** -- edge cases, adversarial inputs
4. **User complaints** -- real-world quality issues

### Graduation Pattern

Capability evals that consistently pass become regression evals:

1. Write a capability eval for a new feature (see `ERROR_ANALYSIS_RECIPE.md`)
2. Run it repeatedly as the feature matures
3. When it passes 10+ times consecutively, promote to `evals/regression/`
4. Mark with `@pytest.mark.regression` and keep the case deterministic
5. Run opt-in: `make eval-regression` (must maintain ~100% pass rate)

**Not** preference graduation: `learning/graduation.py` promotes stable
**preferences** through check-ins to a global profile. It does **not**
implement capability→regression **eval** file promotion.

Default generated CI does **not** block on the full `evals/regression/`
suite; wire that only deliberately (High-tier if you edit default workflows).
Existing golden / public-corpus gates are separate.

---

## Running Evals

`pytest evals/` collects and runs the full eval suite in a generated
project -- no API keys or network needed (LLM-dependent evals are skipped
unless `EVAL_USE_REAL_LLM=1`).

```bash
make eval              # Run all evals (pytest evals/ -- mock LLM by default)
make eval-regression   # Opt-in graduated regression suite (no-op if empty)
EVAL_USE_REAL_LLM=1 make eval  # Run with real LLM (needs API key)
```

See also: [`ERROR_ANALYSIS_RECIPE.md`](ERROR_ANALYSIS_RECIPE.md) (factory-floor
failure → task → regression).

---

## Injection-Defense Golden Set (regression smoke set)

`tasks/test_injection_defense_golden.py` is a deterministic regression smoke
set for the **first two injection-defense layers only**: Layer 1 static
patterns (`security/prompt_guard.py`) and Layer 2 normalization/decoding
(`security/injection_defense.py`). It runs a labeled dataset through the *same*
functions the gateway calls (imported, not reimplemented), tallies false
positives and false negatives per attack category, and compares them to a
committed baseline.

What it is **not**: it is not a security benchmark, and it does **not** cover
Layer 3 (the Sentinel semantic screen), which needs an LLM and lives in the
deliberation, not in a deterministic filter. A green run means "the
deterministic layers still behave as they did when the baseline was frozen," not
"injection is solved."

Dataset (`fixtures/injection_defense_dataset.json`): malicious cases are **not**
copied here -- they reference the shared adversarial corpus
(`tests/adversarial_payloads.py`) by category/index via each case's `source`
field, so there is one payload list, not two. Benign look-alikes (legitimate
text that mentions "ignore", "system prompt", `[INST]`, base64, etc.) carry
literal `input` text and are labeled `pass` to measure false positives.

The baseline deliberately freezes a handful of benign **false positives**: the
affected cases quote a detection pattern or a model control token verbatim
(e.g. citing the regex `ignore all previous instructions`, or documenting
`<|im_start|>`), and a regex layer cannot distinguish *quoting* a pattern from
*using* one. Each such case carries a `note` field in the dataset explaining
why. The baseline guards that this set does not grow; shrinking it (smarter
matching) is an improvement worth a deliberate rebaseline.

The `benign_encoded` category measures the false-positive surface of the
Layer 2 decoding pass on legitimate encoded content -- data URIs, JWTs, hex
digests, base64 config blobs -- as seen on the surfaces that run the full
`advanced=True` scan (MCP tool output, remote-agent responses, knowledge
writes). Measured rate at freeze: 1 of 7 (a base64 YAML blob whose decoded
`system:` key matches the Layer 1 system-role pattern). Because detection is
log-only on those surfaces, an FP costs a warning log line, never a blocked
flow; these cases document the trade-off rather than gate it.

Two equivalent ways to run it:

```bash
# Standalone (what CI calls): prints the per-category FP/FN table, runs the
# dataset-schema preflight first, and exits nonzero on a schema error or a
# regression vs the committed baseline:
python evals/tasks/test_injection_defense_golden.py

# Via pytest: the module's test functions (test_dataset_schema_valid,
# test_corpus_imports_resolve, test_no_regression_vs_baseline) are collected
# with the rest of the suite:
pytest evals/
```

Use the standalone command when you want the per-category table; use
`pytest evals/` when you want the golden set alongside every other eval.

### Updating the baseline (intentional)

The baseline (`fixtures/injection_defense_baseline.json`) is the frozen
per-category FP/FN counts. Regenerate it **only** when you have deliberately
changed the deterministic defenses (added a pattern, adjusted normalization) or
edited the dataset, and you have reviewed the new numbers:

```bash
python evals/tasks/test_injection_defense_golden.py --update-baseline
git add evals/fixtures/injection_defense_baseline.json
# Commit with a message explaining WHY the numbers moved.
```

Treat a baseline change like a snapshot update: the diff should be explainable.
An unexplained rise in false negatives means a defense regressed.

---

## Public-corpus harness (pinned subsets)

`tasks/test_public_corpus_harness.py` grades offline fixtures
(`fixtures/public_corpus_*.json`): 60 InjecAgent + 60 AgentDojo + 30
open-corpus continuity cases through the same Layer 1–2 detectors. Hybrid
compare vs `public_corpus_baseline.json`: landmark FN/FP freezes, non-landmark
FN rise only fails above 5pp, any FP rise fails.

**Non-Claim:** Not a security benchmark; does not measure Layer 3; not an
end-to-end AgentDojo or InjecAgent score; absolute catch rates are advisory
until graduation (3 consecutive green CI runs on `main` at the same baseline
SHA with variance ≤2pp). Attribution: `fixtures/ATTRIBUTION.md`.

```bash
python evals/tasks/test_public_corpus_harness.py
python evals/tasks/test_public_corpus_harness.py --update-baseline  # intentional only
```

---

## Directory Structure

```
evals/
  graders/
    code_grader.py      # Deterministic checks
    model_graders.py    # LLM-as-judge
    human_grader.py     # Manual review interface
  tasks/
    test_security_evals.py     # Security capability evals
    test_quality_evals.py      # Output quality evals
    test_reliability_evals.py  # Reliability and consistency evals
    test_system_evals.py       # System integration evals
    test_injection_defense_golden.py  # Deterministic injection-defense regression smoke set
    test_public_corpus_harness.py     # Pinned public-corpus Layer 1-2 measurement
    corpus_resolve.py                 # Public-corpus schema helpers (local)
  fixtures/
    sample_inputs.json  # Example inputs for evals
    injection_defense_dataset.json    # Labeled golden-set cases (corpus refs + benign look-alikes)
    injection_defense_baseline.json   # Frozen per-category FP/FN baseline
    public_corpus_manifest.json       # Pinned SHAs + licenses + stratification
    public_corpus_cases.json          # 150 offline cases (per-case sha256)
    public_corpus_baseline.json       # Frozen per-category FP/FN baseline
    error_analysis_example.json       # Factory-floor error-analysis recipe fixture
  regression/           # Graduated evals (opt-in via make eval-regression)
    test_isa_open_claim_regression.py  # Worked example: open required ISA claim
  results/              # Eval run results
  human_review/         # Pending human reviews
  ERROR_ANALYSIS_RECIPE.md  # Failure → task → regression recipe
  error_analysis.py         # Structured FailureMode helper
```

### Non-Claims (eval kit)

- Factory-floor / scaffold hygiene only — domain product datasets stay on your
  project.
- LLM-as-judge / model-based graders are **not** default CI for the regression
  recipe; the shipped worked example is code-graded and offline.
- Preference `learning/graduation.py` is not eval graduation (see above).
- `make eval-regression` is opt-in and does not imply every default CI push
  runs the full graduated suite.
