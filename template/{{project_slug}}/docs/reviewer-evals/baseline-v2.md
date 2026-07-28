# Reviewer-eval baseline v2

**Status:** attributable evaluation record. **No promotions.**
Every prompt reviewer remains `SHADOW` in
[`docs/REVIEWER_ASSURANCE.md`](../REVIEWER_ASSURANCE.md).

This file ships with generated projects so the SHADOW-first default is
attributable. The upstream template evaluation that produced it lives
in the roundtable repository at the same relative path.

**Decision:** fixture/prompt gap closure only. Any `SHADOW` →
`BLOCKING` flip requires a follow-up change with recorded human
approval in the promotion-record table — agents cannot self-promote.

## Attribution

| Field | Value |
|-------|-------|
| Evaluation date | 2026-07-27 |
| Fixture path | `reviewer-evals/cases.json` |
| Fixture-set content SHA-256 | `bef282a8aa7a938c862bbee7a4ab6e908d9bf9fd86c10434402664aab6febadb` |
| Runner | `scripts/reviewer_eval.py` SHA-256 `46bc4c4f6d7fc686fb60a2bd23d7ae45c61f479c48fd05fe01e29d7e230955ec` |
| Scanner | `scripts/red_team_check.py` SHA-256 `846127fcf46b212ec450a1d900009c3137a37a80e8e199aaa844d59113f5ecce` |

## Corpus

16 cases across 8 domains (3 `DETERMINISTIC`, 5 `MANUAL_AGENT`
including `reviewer_injection_resistance` for gate 4). Deterministic
runner: **PASS** (6/6) at evaluation time.

## Scorecard summary

| Reviewer | Baseline v2 |
|----------|-------------|
| Security-scoped prompt reviewers with MANUAL_AGENT coverage | Remain `SHADOW` (materially edited rows bumped to `v1`) — gate 4 fixtures seeded; human certification of injection resistance still required before any promotion candidate |
| `solution-architect` | **`NOT_EVALUATED`** (no domain fixtures) — not a vacuous pass |
| `test-architect` | **`NOT_EVALUATED`** (no domain fixtures) — not a vacuous pass |

Promotion-record table stays empty (`_(none yet)_`).

## Non-claims

- CI does not run prompt reviewers.
- This document does not grant `BLOCKING` authority.
- Empty domain coverage is never treated as gate-1 success.

Guidance verified: 2026-07.
