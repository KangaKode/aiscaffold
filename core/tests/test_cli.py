import subprocess

import typer

from aiscaffold import cli


def test_init_trusts_copier_tasks(monkeypatch):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.init(name="my-project", template=cli.TEMPLATE_REPO)

    assert calls == [
        (
            [
                "copier",
                "copy",
                cli.TEMPLATE_REPO,
                ".",
                "--trust",
                "--data",
                "project_name=my-project",
                "--data",
                "project_slug=my_project",
            ],
            True,
        )
    ]


def test_init_does_not_trust_unrecognized_template(monkeypatch):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.init(name="my-project", template="gh:attacker/evil-template")

    assert calls == [
        (
            [
                "copier",
                "copy",
                "gh:attacker/evil-template",
                ".",
                "--data",
                "project_name=my-project",
                "--data",
                "project_slug=my_project",
            ],
            True,
        )
    ]


def test_init_does_not_trust_vcs_like_source_that_resolves_locally(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    local_template = tmp_path / "local-template"
    local_template.mkdir()
    vcs_parent = tmp_path / "gh:attacker"
    vcs_parent.mkdir()
    (vcs_parent / "evil-template").symlink_to(local_template, target_is_directory=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "LOCAL_TEMPLATE", str(local_template))
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.init(name="my-project", template="gh:attacker/evil-template")

    assert calls == [
        (
            [
                "copier",
                "copy",
                "gh:attacker/evil-template",
                ".",
                "--data",
                "project_name=my-project",
                "--data",
                "project_slug=my_project",
            ],
            True,
        )
    ]


def test_init_does_not_trust_gitlab_shortcut_that_resolves_locally(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    local_template = tmp_path / "local-template"
    local_template.mkdir()
    vcs_parent = tmp_path / "gl:attacker"
    vcs_parent.mkdir()
    (vcs_parent / "evil-template").symlink_to(local_template, target_is_directory=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "LOCAL_TEMPLATE", str(local_template))
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.init(name="my-project", template="gl:attacker/evil-template")

    assert "--trust" not in calls[0][0]


def test_init_does_not_trust_git_plus_url_that_resolves_locally(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    local_template = tmp_path / "local-template"
    local_template.mkdir()
    vcs_parent = tmp_path / "git+https:"
    vcs_parent.mkdir()
    (vcs_parent / "evil.example").symlink_to(local_template, target_is_directory=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "LOCAL_TEMPLATE", str(local_template))
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.init(name="my-project", template="git+https://evil.example")

    assert "--trust" not in calls[0][0]


def test_init_rejects_names_that_cannot_form_safe_slug(monkeypatch):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    try:
        cli.init(name='bad"; touch /tmp/pwned #', template=cli.TEMPLATE_REPO)
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("expected init to reject unsafe project name")

    assert calls == []


def test_update_trusts_copier_tasks(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(f"_src_path: {cli.TEMPLATE_REPO}\n")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.update()

    assert calls == [(["copier", "update", "--trust"], True)]


def test_update_rejects_untrusted_answer_source(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text("_src_path: gh:attacker/evil-template\n")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    try:
        cli.update()
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("expected update to reject untrusted template source")

    assert calls == []


def test_update_rejects_duplicate_answer_sources(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(
        f"_src_path: {cli.TEMPLATE_REPO}\n_src_path: gh:attacker/evil-template\n"
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    try:
        cli.update()
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("expected update to reject ambiguous template source")

    assert calls == []


def test_update_rejects_tagged_duplicate_answer_sources(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(
        f"_src_path: {cli.TEMPLATE_REPO}\n!!str _src_path: gh:attacker/evil-template\n"
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    try:
        cli.update()
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("expected update to reject YAML-tagged duplicate source")

    assert calls == []


def test_update_rejects_explicit_string_tag_source_key(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(
        f"!!str _src_path: {cli.TEMPLATE_REPO}\n"
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    try:
        cli.update()
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("expected update to reject explicitly tagged source key")

    assert calls == []


def test_update_rejects_explicit_string_tag_source_value(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(
        f"_src_path: !!str {cli.TEMPLATE_REPO}\n"
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    try:
        cli.update()
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("expected update to reject explicitly tagged source value")

    assert calls == []


def test_update_rejects_merge_answer_sources(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(
        "defaults: &defaults\n"
        "  _src_path: gh:attacker/evil-template\n"
        f"_src_path: {cli.TEMPLATE_REPO}\n"
        "<<: *defaults\n"
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    try:
        cli.update()
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("expected update to reject YAML merge source")

    assert calls == []


def test_update_rejects_complex_answer_source_key(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text("? _src_path\n: gh:attacker/evil-template\n")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    try:
        cli.update()
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("expected update to reject complex YAML source key")

    assert calls == []


def test_update_rejects_tagged_answer_source_key(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(
        f"!custom _src_path: {cli.TEMPLATE_REPO}\n"
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    try:
        cli.update()
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("expected update to reject tagged YAML source key")

    assert calls == []


def test_update_rejects_indented_explicit_answer_source_key(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(" ? _src_path\n : gh:attacker/evil-template\n")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    try:
        cli.update()
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("expected update to reject indented complex YAML source key")

    assert calls == []


def test_update_rejects_source_with_surrounding_whitespace(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(
        f'_src_path: " {cli.TEMPLATE_REPO} "\n'
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    try:
        cli.update()
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("expected update to reject whitespace-padded template source")

    assert calls == []


def test_init_surfaces_copier_failures(monkeypatch):
    def fake_run(cmd, check):
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    try:
        cli.init(name="my-project", template=cli.TEMPLATE_REPO)
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("expected init to exit when copier fails")
