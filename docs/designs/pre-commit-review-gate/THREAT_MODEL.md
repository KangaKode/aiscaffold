# Threat Model: Pre-commit Review Gate (+ native hooks)

**Asset:** Integrity of the Roundtable git history — commits should not land without Bugbot + Security Review on the same tree.

**Attacker:** Local developer or coding agent with shell access to the checkout (trusted-developer threat model; not remote multi-tenant).

**Posture:** Cursor `beforeShellExecution` is **defense-in-depth**. With `core.hooksPath=.githooks` enabled (`make hooks-install`), the native `pre-commit` hook re-checks the receipt at commit time for any client that honors git hooks.

## Abuse cases and mitigations

| ID | Abuse case | Severity | Mitigation | Residual |
|----|------------|----------|------------|----------|
| T1 | Skip reviews, lone `git commit` / `git -C` / `git -c` in Cursor | High | Cursor hook + native `pre-commit` `--check` | — when hooksPath set |
| T2 | Compound `… && git commit` (TOCTOU) | High | Cursor denies compound; native hook re-checks **after** mutations at commit | — when hooksPath set |
| T3 | Stage v1, edit worktree to v2, commit index | High | `--check` requires no unstaged tracked diffs; fingerprint path→bytes | — |
| T4 | `git -C otherrepo commit` using this receipt | High | Cursor enforces git-common-dir; native hook uses committing toplevel | — |
| T5 | Linked worktree ≠ hook install path | High | Cursor `cwd=target`; native hook uses that worktree toplevel | — |
| T6 | `git --git-dir=…` / glued `-Cpath` / `GIT_DIR=` / `GIT_WORK_TREE=` | Medium | Cursor denies unsupported forms | Exotic env still possible if hooks skipped |
| T7 | Self-attested receipt without real reviews | Medium | Documented; skills + process | Any local actor can forge receipt |
| T8 | `python -c` / wrapper invoking `git commit` | Medium | **Native hook** runs if wrapper still calls `git commit` | Wrappers that bypass git hooks |
| T9 | Commit outside Cursor (plain terminal) | Medium | **Native hook** when hooksPath installed | Clone without `make hooks-install` |
| T10 | `--no-verify` | Low/Medium | Process forbids | Still bypasses native hook |
| T11 | `ROUNDTABLE_SKIP_REVIEW_RECEIPT=1` | Accepted | Documented emergency bypass (Cursor + native) | Human audit |
| T12 | Invalid Cursor hook JSON / crash | High | Fail-closed deny / exit 2 | — |
| T13 | `git merge` / `cherry-pick` / `rebase --continue` | High | **Native pre-commit** runs on resulting commit | Tree may need fresh reviews after merge |
| T14 | `git` alias hiding `commit` | Medium | Native hook still runs on the commit | Cursor may miss alias forms |

## Non-goals

- Cryptographic proof that Bugbot/Security models ran.
- Replacing CI (`validate_generated.sh`, Gitleaks, pip-audit).
- Multi-user attestation.
- Forcing `core.hooksPath` via committed git config (must be per-clone `make hooks-install`).

## Install expectation

Contributors run `make hooks-install` once per clone. CI and hosts that never commit locally are unaffected.
