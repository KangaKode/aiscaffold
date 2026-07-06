# Updating Generated Projects

The copier destination directory is the generated project root: the git
repo, `.copier-answers.yml` (the recorded template version and answers),
and the project files all live together. That layout plus the answers
file is what makes `copier update` work — keep `.copier-answers.yml`
committed.

## For project owners

From inside a generated project (a git repo with a clean working tree):

```bash
pip install copier
copier update --trust
```

Copier re-renders the project from the latest template release, replays
your recorded answers, and produces a three-way merge against your local
changes. Review the diff, resolve any conflicts, then run the project's
own validation (`make test`) before committing.

To change an answer during the update (for example, turning the API
gateway off), pass it explicitly:

```bash
copier update --trust --data include_api_gateway=false
```

Note: `--trust` is required because this template defines `_tasks`
(post-generation commands such as `git init`). Review `copier.yml` if
you want to see exactly what runs.

## For template maintainers: tag releases

`copier update` resolves "latest" from **git tags** on the template
repository, not from the tip of `main`. Untagged commits are invisible
to updating projects. So:

- Tag every release with a PEP 440 / semver-style version: `git tag v0.2.0 && git push --tags`.
- Tag from `main` only after the full validation pipeline
  (`bash scripts/validate_generated.sh`) is green.
- Treat answer/question changes (renamed or removed questions in
  `copier.yml`) as breaking: document them in the release notes so
  projects updating across that boundary know what to expect.

Projects can pin a specific version with `copier update --vcs-ref v0.2.0`.
