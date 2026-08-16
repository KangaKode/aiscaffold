"""Regression tests for pre-commit review receipt + Cursor gate parsing."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RECEIPT_SCRIPT = REPO_ROOT / "scripts" / "record_review_receipt.py"
HOOK_SCRIPT = REPO_ROOT / ".cursor" / "hooks" / "require-pre-commit-reviews.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TreeFingerprintStagingStable(unittest.TestCase):
    def test_git_add_new_file_does_not_change_fingerprint(self) -> None:
        receipt = _load("record_review_receipt", RECEIPT_SCRIPT)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.check_call(["git", "init"], cwd=root)
            subprocess.check_call(
                ["git", "config", "user.email", "test@example.com"], cwd=root
            )
            subprocess.check_call(
                ["git", "config", "user.name", "Test"], cwd=root
            )
            (root / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.check_call(["git", "add", "tracked.txt"], cwd=root)
            subprocess.check_call(
                ["git", "commit", "-m", "init"], cwd=root
            )
            new = root / "brand_new.py"
            new.write_text("print('hi')\n", encoding="utf-8")
            before = receipt.tree_fingerprint(root)
            subprocess.check_call(["git", "add", "brand_new.py"], cwd=root)
            after = receipt.tree_fingerprint(root)
            self.assertEqual(before, after)


class AnalyzeCommandGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.hook = _load("require_pre_commit_reviews", HOOK_SCRIPT)

    def test_commit_message_with_and_does_not_deny(self) -> None:
        action, _ = self.hook.analyze_command(
            'git commit -m "fix && ship"',
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(action, "check")

    def test_compound_commit_is_ambiguous(self) -> None:
        action, _ = self.hook.analyze_command(
            "git add . && git commit -m msg",
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(action, "ambiguous")

    def test_git_c_flag_is_check(self) -> None:
        action, target = self.hook.analyze_command(
            "git -c core.pager=cat commit -m msg",
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(action, "check")
        self.assertEqual(target, REPO_ROOT.resolve())

    def test_command_substitution_git_is_ambiguous(self) -> None:
        action, _ = self.hook.analyze_command(
            "git $(echo commit) -m msg",
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(action, "ambiguous")

    def test_quoted_commit_message_substitution_is_check(self) -> None:
        action, _ = self.hook.analyze_command(
            'git commit -m "$(cat /tmp/msg.txt)"',
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(action, "check")

    def test_multiline_unquoted_git_is_ambiguous(self) -> None:
        action, _ = self.hook.analyze_command(
            "git\ncommit -m msg",
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(action, "ambiguous")

    def test_heredoc_mentioning_git_without_substitution_on_git_allows(self) -> None:
        # Agent tooling often embeds the word "git" in PR bodies via files;
        # substitution alone must not deny every shell that mentions git.
        action, _ = self.hook.analyze_command(
            "gh pr create --body-file /tmp/body.md",
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(action, "allow")

    def test_git_dir_env_is_unsupported(self) -> None:
        action, _ = self.hook.analyze_command(
            "GIT_DIR=/tmp/other/.git git commit -m msg",
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(action, "unsupported")

    def test_non_commit_allows(self) -> None:
        action, _ = self.hook.analyze_command(
            "git status",
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(action, "allow")

    def test_hooks_json_has_no_matcher(self) -> None:
        data = json.loads(
            (REPO_ROOT / ".cursor" / "hooks.json").read_text(encoding="utf-8")
        )
        hooks = data["hooks"]["beforeShellExecution"]
        self.assertTrue(hooks)
        for entry in hooks:
            self.assertNotIn("matcher", entry)


if __name__ == "__main__":
    unittest.main()
