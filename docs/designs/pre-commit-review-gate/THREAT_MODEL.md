# Threat Model: Pre-commit Review Gate

**Asset:** Integrity of the Roundtable git history — commits should not land without Bugbot + Security Review on the same tree.

**Attacker:** Local developer or coding agent with shell access to the checkout (trusted-developer threat model; not remote multi-tenant).

**Posture:** This PR is **defense-in-depth for Cursor shell `git commit`**, not fail-closed commit integrity. Native git hooks are a required follow-up.

## Abuse cases and mitigations

| ID | Abuse case | Severity | Mitigation in this PR | Residual |
|----|------------|----------|----------------------|----------|
| T1 | Skip reviews, lone `git commit` / `git -C` / `git -c` in Cursor | High | Hook (no brittle matcher) denies without receipt | — |
| T2 | Compound `… && git commit` (TOCTOU) | High | Compound / multi-`git` commands **deny** (fail closed); `-m` text tokenized so messages are not false denials | Mutations still possible via wrappers; native hook re-checks at commit time |
| T3 | Stage v1, edit worktree to v2, commit index | High | `--check` requires no unstaged tracked diffs; fingerprint is path→bytes (staging-stable) | — |
| T4 | `git -C otherrepo commit` using this receipt | High | Enforce only when `--git-common-dir` matches Roundtable | — |
| T5 | Linked worktree ≠ hook install path | High | Compare `--git-common-dir`; run `--check` with `cwd=target` toplevel | — |
| T6 | `git --git-dir=…` / glued `-Cpath` / `GIT_DIR=` / `GIT_WORK_TREE=` env forms | Medium | Unsupported forms **deny** | — |
| T7 | Self-attested receipt without real reviews | Medium | Documented; skills + process require honest recording | Any local actor can forge receipt |
| T8 | `python -c` / wrapper invoking `git commit` | Medium | Not covered by Cursor shell hook argv | **Native `.git/hooks/pre-commit` follow-up** |
| T9 | Commit outside Cursor (plain terminal) | Medium | Project hook only fires in Cursor | Native git hook follow-up |
| T10 | `--no-verify` | Low/Medium | Cursor hook still sees `git commit` | Terminal without Cursor |
| T11 | `ROUNDTABLE_SKIP_REVIEW_RECEIPT=1` | Accepted | Documented emergency bypass | Human audit |
| T12 | Invalid hook JSON / crash | High | Fail-closed deny / exit 2 with `failClosed: true` | — |
| T13 | `git merge` / `cherry-pick` / `rebase --continue` | High | Not treated as `git commit` by gate | Native hook / broader matchers follow-up |
| T14 | `git` alias hiding `commit` | Medium | Tokenization looks for `commit` subcommand | Native hook follow-up |

## Non-goals

- Cryptographic proof that Bugbot/Security models ran.
- Replacing CI (`validate_generated.sh`, Gitleaks, pip-audit).
- Multi-user attestation.
- Claiming fail-closed integrity against a shell-capable agent (Phase 3 native hooks).

## Follow-up (required for enforcement claims)

Ship a native `pre-commit` (or `core.hooksPath`) that calls `scripts/record_review_receipt.py --check` so T8/T9/T13 close regardless of Cursor, and compound TOCTOU is re-checked at commit time.
