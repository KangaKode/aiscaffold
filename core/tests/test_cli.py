import subprocess

import typer

from aiscaffold import cli


def test_init_trusts_default_copier_tasks(monkeypatch):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_get_template_source", lambda: cli.TEMPLATE_REPO)

    cli.init(name="my-project", template=None)

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
            ],
            True,
        )
    ]


def test_init_does_not_trust_custom_template(monkeypatch):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.init(name="my-project", template="/tmp/template")

    assert calls == [
        (
            [
                "copier",
                "copy",
                "/tmp/template",
                ".",
                "--data",
                "project_name=my-project",
            ],
            True,
        )
    ]


def test_update_trusts_official_source_but_skips_tasks(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(
        f"_src_path: {cli.TEMPLATE_REPO}\n"
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.update()

    assert calls == [(["copier", "update", "--skip-tasks", "--trust"], True)]


def test_update_rejects_untrusted_source(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text("_src_path: /tmp/template\n")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    try:
        cli.update()
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("expected update to exit for untrusted template")

    assert calls == []


def test_init_surfaces_copier_failures(monkeypatch):
    def fake_run(cmd, check):
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    try:
        cli.init(name="my-project", template="/tmp/template")
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:
        raise AssertionError("expected init to exit when copier fails")
