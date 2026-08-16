# Native git hooks (review receipt)

This directory is intended for `core.hooksPath` (not the Python
[`pre-commit`](https://pre-commit.com) framework in `.pre-commit-config.yaml`).

## Enable (per clone)

```bash
make hooks-install
# equivalent: git config core.hooksPath .githooks
```

`pre-commit` runs `python3 scripts/record_review_receipt.py --check`, then
(if the `pre-commit` CLI is installed) chains into the Python
[pre-commit](https://pre-commit.com) framework via `pre-commit hook-impl` so
`core.hooksPath=.githooks` does not disable `.pre-commit-config.yaml`.

## Framework hooks

If `.pre-commit-config.yaml` exists, this hook requires a resolvable
`pre-commit` CLI (`PATH`, `.venv/bin/pre-commit`, or `python3 -m pre_commit`)
and chains `pre-commit hook-impl`. Missing CLI → commit fails with a warning
(so `core.hooksPath` cannot silently drop framework checks).

- `ROUNDTABLE_SKIP_REVIEW_RECEIPT=1` — emergency skip (audited locally)
- `git commit --no-verify` — still possible; process forbids it for Roundtable work
