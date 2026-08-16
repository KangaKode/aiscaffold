# Workflow States: Pre-commit Review Gate

```text
[dirty worktree]
      │
      ▼
[run Bugbot] ──record──► receipt.reviews.bugbot
      │
      ▼
[run Security Review] ──record──► receipt.reviews.security-review
      │
      ▼
[git add]  (index must match worktree; fingerprint unchanged if content same)
      │
      ▼
[git commit] ──hook──► --check
      │                    │
      │                    ├─ missing/stale/missing reviewer ──► DENY
      │                    ├─ unstaged tracked diffs ──► DENY
      │                    └─ OK ──► ALLOW (normal git hooks still run)
      ▼
[committed]
```

## Operator states

| State | Meaning |
|-------|---------|
| No receipt | Commit denied |
| Partial receipt (one reviewer) | Commit denied |
| Stale fingerprint | Diff changed after record; re-run both reviews |
| Emergency bypass | `ROUNDTABLE_SKIP_REVIEW_RECEIPT=1` (documented; audit by humans) |
| Unsupported `git` form | `--git-dir` / glued `-C` denied |
