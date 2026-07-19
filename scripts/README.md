# Maintainer scripts (repo root)

These helpers live at the roundtable repo root and **do not** ship into
generated projects under `template/{{project_slug}}/scripts/`.

## Public-corpus refresh

Offline fixtures under `template/{{project_slug}}/evals/fixtures/public_corpus_*.json`
are pinned subsets of InjecAgent + AgentDojo (+ open-corpus continuity).
Refresh is maintainer-only, zero-egress by default, and refuses in CI.

**v1 helper is verify + optional upstream probe only — it never writes fixture
JSON.** Case reselection is manual; after editing cases, re-grade and commit
fixtures + baseline + `evals/fixtures/ATTRIBUTION.md` together.

```bash
# From roundtable repo root, never in CI:
PUBLIC_CORPUS_REFRESH=1 python scripts/refresh_public_corpus.py --dry-run
PUBLIC_CORPUS_REFRESH=1 python scripts/refresh_public_corpus.py --confirm --probe-network
# After intentional reselection:
python <generated>/evals/tasks/test_public_corpus_harness.py --update-baseline
```

Requires `PUBLIC_CORPUS_REFRESH=1`. Exits nonzero if `CI` or `GITHUB_ACTIONS` is set.
