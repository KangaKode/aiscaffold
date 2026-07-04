"""Regression tests for Copier configuration safety."""

from pathlib import Path
import unittest

import yaml
from jinja2 import Environment, StrictUndefined


REPO_ROOT = Path(__file__).resolve().parents[1]


class CopierSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = yaml.safe_load((REPO_ROOT / "copier.yml").read_text(encoding="utf-8"))
        cls.jinja = Environment(undefined=StrictUndefined)

    def render_validator(self, question: str, value: str) -> str:
        validator = self.config[question]["validator"]
        return self.jinja.from_string(validator).render({question: value}).strip()

    def render_project_slug_default(self, project_name: str) -> str:
        default = self.config["project_slug"]["default"]
        return self.jinja.from_string(default).render({"project_name": project_name}).strip()

    def test_project_name_rejects_shell_metacharacters(self):
        self.assertEqual("", self.render_validator("project_name", "My AI Tool"))
        self.assertEqual("", self.render_validator("project_name", "safe-project_2"))
        self.assertTrue(self.render_validator("project_name", 'Safe"; touch /tmp/pwned #'))
        self.assertTrue(self.render_validator("project_name", "$(touch /tmp/pwned)"))

    def test_project_slug_rejects_shell_metacharacters(self):
        self.assertEqual("", self.render_validator("project_slug", "safe_project"))
        self.assertTrue(self.render_validator("project_slug", 'safe_project"; touch /tmp/pwned #'))

    def test_project_slug_default_is_validator_safe(self):
        for project_name, expected_slug in [
            ("2024 Demo", "project_2024_demo"),
            ("v1.0 Release", "v1_0_release"),
        ]:
            with self.subTest(project_name=project_name):
                slug = self.render_project_slug_default(project_name)
                self.assertEqual(expected_slug, slug)
                self.assertEqual("", self.render_validator("project_slug", slug))

    def test_layers_reject_shell_metacharacters_and_path_traversal(self):
        self.assertEqual("", self.render_validator("layers", "data,analysis,components"))
        self.assertTrue(self.render_validator("layers", 'data"; touch /tmp/pwned #'))
        self.assertTrue(self.render_validator("layers", "data,../secrets"))

    def test_tasks_do_not_render_project_name_into_shell_commands(self):
        tasks = "\n".join(self.config["_tasks"])
        self.assertNotIn("{{ project_name }}", tasks)

    def test_cli_uses_trust_for_template_tasks(self):
        cli_source = (REPO_ROOT / "core" / "src" / "aiscaffold" / "cli.py").read_text(encoding="utf-8")
        self.assertIn('cmd.append("--trust")', cli_source)
        self.assertIn('["copier", "update", "--trust"]', cli_source)


if __name__ == "__main__":
    unittest.main()
