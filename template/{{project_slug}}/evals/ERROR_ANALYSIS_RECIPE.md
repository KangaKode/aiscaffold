# Error-analysis recipe (factory floor)

**Scope:** Scaffold / generated-project **factory floor** only — not domain
product eval datasets. No new tracing stack; consume existing audit, metrics,
or phase artifacts (fixtures are fine).

**Related:** Task ISA claim-closure (`orchestration/task_isa.py`,
`isa_closure` on round-table results) is detect-only in Phase 1 — open or
unverifiable required claims are a measurable failure mode for evals, not a
consensus refuse. Preference graduation (`learning/graduation.py`) is a
**different** path (cross-project preferences via check-ins); it does **not**
implement capability→regression eval promotion.

## Steps

1. **Capture a failure** from deliberation audit metadata, metrics counters,
   a phase artifact snippet, or a synthetic fixture under `evals/fixtures/`.
2. **Record given / expected / actual** (and optional `source`) as a JSON
   object. Required keys: `failure_id`, `given`, `expected`, `actual`.
3. **Run the helper** (deterministic, no network):

   ```python
   from pathlib import Path
   from evals.error_analysis import analyze_failure_file

   mode = analyze_failure_file(Path("evals/fixtures/error_analysis_example.json"))
   assert mode.suggested_task_stub.startswith("evals/tasks/")
   ```

4. **Add or update** an `evals/tasks/` case that fails until the mode is fixed
   (or documents the promotion path in the docstring).
5. **Graduate** when the capability case is stable: move or mirror under
   `evals/regression/`, mark `@pytest.mark.regression`, keep it code-graded
   and offline.
6. **Run opt-in:** `make eval-regression` (no-op exit 0 if no `test_*.py`
   under `evals/regression/`). Default generated CI workflows do **not**
   block on this suite.

## Worked example

Fixture: `fixtures/error_analysis_example.json` → task recipe test
`tasks/test_error_analysis_recipe.py` → graduated regression
`regression/test_isa_open_claim_regression.py` (open required ISA claim
detected via `evaluate_isa_closure`).

## Non-Claims

- Factory floor only; domain datasets stay on your generated project.
- LLM-as-judge / model-based graders are **not** part of default CI for this
  recipe; the worked example is code-graded.
- `make eval-regression` is opt-in; it does not replace golden/public-corpus
  gates already wired elsewhere.
