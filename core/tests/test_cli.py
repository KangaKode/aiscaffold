import subprocess

import typer

from aiscaffold import cli


def test_init_trusts_copier_tasks(monkeypatch):
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
                "--trust",
                "--data",
                "project_name=my-project",
            ],
            True,
        )
    ]


def test_update_trusts_template_but_skips_generation_tasks(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text("_src_path: /tmp/template\n")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.update()

    assert calls == [(["copier", "update", "--trust", "--skip-tasks"], True)]


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
