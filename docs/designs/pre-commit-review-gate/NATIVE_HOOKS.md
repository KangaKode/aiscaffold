# Design delta: Native pre-commit hooks

**Risk-tier:** High (follow-up to `pre-commit-review-gate`)  
**Branch:** `feat/native-pre-commit-hooks`  
**Depends on:** receipt script + Cursor gate from `feat/pre-commit-review-gate`

## Intent

Install a repo-managed git hook via `core.hooksPath=.githooks` that runs
`scripts/record_review_receipt.py --check` at commit time. This closes the
Cursor-only and compound-TOCTOU gaps documented in `THREAT_MODEL.md` for the
defense-in-depth Cursor gate.

## Architecture impact

```text
git commit (any client)
  → .githooks/pre-commit
  → record_review_receipt.py --check
  → allow / deny (exit code)
```

Cursor `beforeShellExecution` remains defense-in-depth (early deny, clearer
agent messages). Native hook is the integrity check that applies outside Cursor.

## Data movement

Unchanged fingerprint/receipt path. Hook cwd is the committing worktree
toplevel (`git rev-parse --show-toplevel` inside the hook).

## Failure behavior

- Missing/stale receipt → commit aborted (exit 1)
- Missing script → exit 1
- `ROUNDTABLE_SKIP_REVIEW_RECEIPT` → allow with stdout notice
- `--no-verify` → residual (documented)

## Risks

| Risk | Handling |
|------|----------|
| Developers forget `make hooks-install` | Document in CONTRIBUTING + DEVELOPMENT_PROCESS; CI cannot set local hooksPath |
| Confusion with Python `pre-commit` framework | `.githooks/` name + README; hook chains `pre-commit hook-impl` when CLI present |
| `core.hooksPath` would skip framework hooks | Chain `pre-commit hook-impl` after receipt check |
| Merge/cherry-pick without reviews | Hook runs; may block until receipt matches post-merge tree (honest friction) |

## Planned tests

- Unit: hook script invokes `--check` and respects skip env (subprocess)
- Existing fingerprint / gate tests remain authoritative for receipt logic
