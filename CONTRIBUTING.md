# Contributing

This repo follows a gated review workflow — the full version lives in [docs/DEVELOPMENT_PROCESS.md](docs/DEVELOPMENT_PROCESS.md). The short version:

1. **Plan first.** Open an issue or draft describing the change. Non-trivial work produces the design artifacts required by the process doc (architecture map, data flow, wireframes or workflow states) and gets a design review (security, operations, and template-DX perspectives as relevant) before implementation starts. Tests are planned before production logic.
2. **Branch and keep the PR focused.** One change per PR; no direct commits to `main`.
3. **Validate before requesting review.** `bash scripts/validate_generated.sh` must pass completely — it generates a project from the template and runs the full 17-check pipeline (tests, lint, security scans, red-team checks, the injection-defense golden set). The script exits 0 even when it prints warnings; read the output and resolve or justify warnings in the PR.
4. **Update docs in the same PR.** Capability claims cite code and tests; any quoted test/check counts must come from an actual run. Limitations belong in the GOVERNANCE Non-Claims section — never overclaim.
5. **Review findings get regression tests.** A fix should include a test that fails on the pre-fix code.
6. **Squash-merge after approval and green CI.**

Design constraints worth knowing before you write code:

- Shipped defaults detect and flag — they never silently auto-act. Enforcement is opt-in behind env flags.
- Schema changes go through `MIGRATIONS` in `learning/tables.py` (see the baseline-freeze rule documented there).
- Template files containing jinja placeholders need the `.jinja` suffix, or generated projects receive them unrendered.
- Respect module line-cap headers; if a file must grow, update its header honestly in the same PR.
