import subprocess

import typer

from aiscaffold import cli


def test_init_trusts_known_template_tasks(monkeypatch):
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
            ],
            True,
        )
    ]


def test_init_does_not_trust_untrusted_template(monkeypatch):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.init(name="my-project", template="/tmp/untrusted-template")

    assert calls == [
        (
            [
                "copier",
                "copy",
                "/tmp/untrusted-template",
                ".",
                "--data",
                "project_name=my-project",
            ],
            True,
        )
    ]


def test_init_does_not_trust_url_shaped_local_symlink(monkeypatch, tmp_path):
    calls = []
    url_path = tmp_path / "https:" / "evil.example"
    url_path.mkdir(parents=True)
    (url_path / "template").symlink_to(cli.LOCAL_TEMPLATE)

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.init(name="my-project", template="https://evil.example/template")

    assert "--trust" not in calls[0][0]


def test_update_trusts_known_template_tasks(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(
        f"_src_path: {cli.TEMPLATE_REPO}\n"
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.update()

    assert calls == [(["copier", "update", "--trust"], True)]


def test_update_does_not_trust_untrusted_template(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text("_src_path: /tmp/untrusted-template\n")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.update()

    assert calls == [(["copier", "update"], True)]


def test_update_does_not_trust_url_shaped_local_symlink(monkeypatch, tmp_path):
    calls = []
    url_path = tmp_path / "https:" / "evil.example"
    url_path.mkdir(parents=True)
    (url_path / "template").symlink_to(cli.LOCAL_TEMPLATE)

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".copier-answers.yml").write_text(
        "_src_path: https://evil.example/template\n"
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.update()

    assert calls == [(["copier", "update"], True)]


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
