# Data Flow: Pre-commit Review Gate

## Inputs

| Source | Data | Trust |
|--------|------|-------|
| Working tree | Path → worktree bytes for paths differing from HEAD (incl. untracked) | Local developer / agent |
| Index vs worktree | `git diff --name-only` (unstaged check) | Must be empty at commit |
| Review skills | Finding counts written into receipt | Self-attested (see threat model) |

## Outputs

| Sink | Data | Notes |
|------|------|-------|
| `.cursor/review-receipts/pre-commit.json` | `tree_fingerprint`, per-reviewer timestamps + finding counts | Gitignored |
| Hook stdout | Cursor `permission` JSON | Fail-closed on invalid stdin |

## Fingerprint

1. Resolve repo via `git rev-parse --show-toplevel` from process **cwd**
   (linked worktrees fingerprint the tree being committed).
2. Collect sorted unique paths from `git diff --name-only HEAD` plus
   untracked (`??`) paths.
3. For each path, hash the relative path and worktree file bytes (or a
   deletion marker if the path is gone). Do **not** hash patch text —
   so `git add` of a new file does not change the fingerprint.
4. `--check` still requires no unstaged tracked diffs so the index matches
   the reviewed worktree.

## Cross-repo

Hook resolves commit target (`git -C` / toplevel). Enforcement runs only when `git rev-parse --git-common-dir` matches this Roundtable checkout (covers linked worktrees).
