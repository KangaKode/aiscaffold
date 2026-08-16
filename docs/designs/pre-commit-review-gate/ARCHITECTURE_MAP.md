# Architecture Map: Pre-commit Review Gate

**Risk-tier:** High (`.cursor/hooks*`, commit control plane)  
**Status:** Design for implementation on `feat/pre-commit-review-gate`

## Components

| Component | Role |
|-----------|------|
| `review-bugbot` / `review-security` skills | Run Cursor review subagents; record receipts |
| `scripts/record_review_receipt.py` | Fingerprint worktree; write/check `.cursor/review-receipts/pre-commit.json` |
| `.cursor/hooks/require-pre-commit-reviews.py` | `beforeShellExecution` deny `git commit` without valid receipt |
| `.cursor/hooks.json` | Project hook registration (checked into repo) |
| `~/.cursor/hooks.json` | Optional user-local mirror; **not** committed |
| `docs/DEVELOPMENT_PROCESS.md` + `.cursor/rules/development-process.mdc` | Policy: both reviews before commit |

## Non-components (explicit)

- Native `.git/hooks/pre-commit` — recommended follow-up; not in v1 of this PR (closes wrapper / non-Cursor gaps).
- CI replacement — this gate does not replace `validate_generated.sh` or GitHub Actions.

```text
Agent shell ──► beforeShellExecution hook
                      │
                      ├─ not git commit ──► allow
                      ├─ other git repo ──► allow
                      └─ this repo commit ──► record_review_receipt.py --check
                                                ├─ OK ──► allow
                                                └─ fail ──► deny
```
