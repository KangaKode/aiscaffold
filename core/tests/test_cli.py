import subprocess

import typer

from aiscaffold import cli


def test_init_trusts_default_copier_tasks(monkeypatch):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.setattr(cli, "_get_template_source", lambda: "gh:KangaKode/roundtable")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.init(name="my-project", template=None)

    assert calls == [
        (
            [
                "copier",
                "copy",
                "gh:KangaKode/roundtable",
                ".",
                "--trust",
                "--data",
                "project_name=my-project",
            ],
            True,
        )
    ]


def test_init_does_not_trust_custom_template_by_default(monkeypatch):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.init(name="my-project", template="https://example.com/template.git")

    assert calls == [
        (
            [
                "copier",
                "copy",
                "https://example.com/template.git",
                ".",
                "--data",
                "project_name=my-project",
            ],
            True,
        )
    ]


def test_init_trusts_custom_template_when_requested(monkeypatch):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.init(
        name="my-project",
        template="https://example.com/template.git",
        trust=True,
    )

    assert calls == [
        (
            [
                "copier",
                "copy",
                "https://example.com/template.git",
                ".",
                "--trust",
                "--data",
                "project_name=my-project",
            ],
            True,
        )
    ]


def test_update_trusts_official_copier_tasks(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(
        "_src_path: gh:KangaKode/roundtable\n"
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.update()

    assert calls == [(["copier", "update", "--trust"], True)]


def test_update_does_not_trust_custom_template_by_default(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(
        "_src_path: https://example.com/template.git\n"
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.update()

    assert calls == [(["copier", "update"], True)]


def test_update_trusts_custom_template_when_requested(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(
        "_src_path: https://example.com/template.git\n"
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.update(trust=True)

    assert calls == [(["copier", "update", "--trust"], True)]


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
