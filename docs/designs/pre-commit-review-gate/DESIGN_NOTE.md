# Design Note: Pre-commit Review Gate

**Risk-tier:** High  
**Branch:** `feat/pre-commit-review-gate`  
**Companion artifacts:** `ARCHITECTURE_MAP.md`, `DATA_FLOW.md`, `WORKFLOW_STATES.md`, `THREAT_MODEL.md`

## Intent

Require Bugbot **and** Security Review on the current worktree before any `git commit` to this Roundtable checkout, via a Cursor `beforeShellExecution` hook and a gitignored receipt file.

**Honest posture:** defense-in-depth for Cursor `git commit`, not fail-closed integrity. See `THREAT_MODEL.md`. Native `core.hooksPath` / `pre-commit` is the follow-up that closes residual bypasses.

## Split from Task ISA

Task ISA + Capability Doctor ship as a Medium PR without this gate. This High PR owns hooks and process enforcement so ceremony and review stay focused.

## User-local hooks

`~/.cursor/hooks.json` may mirror the project hook for convenience. It is **never** committed to the repository.
