"""Tests for the files other tools run: the GitHub Action and the pre-commit hook.

Neither runs in this suite — GitHub and pre-commit execute them — so these
pin the properties that would otherwise only fail in someone else's CI, and
parse the files with the project's own YAML reader rather than a dependency.
"""

import os
import unittest

from agentsmith.miniyaml import parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(relative):
    with open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
        return parse(handle.read())


class TestGitHubAction(unittest.TestCase):
    def setUp(self):
        self.action = load("action.yml")
        self.steps = self.action["runs"]["steps"]

    def test_is_a_composite_action_with_the_documented_inputs(self):
        self.assertEqual(self.action["runs"]["using"], "composite")
        defaults = {
            name: spec.get("default") for name, spec in self.action["inputs"].items()
        }
        self.assertEqual(
            defaults,
            {"path": ".", "file": "", "strict": "false", "python-version": "3.12"},
        )
        self.assertTrue(self.action["branding"].get("icon"))
        self.assertTrue(self.action["branding"].get("color"))

    def test_inputs_never_reach_a_script_as_expressions(self):
        """`${{ inputs.file }}` inside `run:` is pasted into the script before
        bash parses it, so a crafted value would run as a command. Inputs go
        through `env:` instead, where they stay data."""
        scripts = [step["run"] for step in self.steps if "run" in step]
        self.assertEqual(len(scripts), 2)
        for script in scripts:
            self.assertNotIn("${{", script)
        env = self.steps[-1]["env"].values()
        for name in ("path", "file", "strict"):
            self.assertTrue(any("inputs.%s " % name in value for value in env), name)

    def test_installs_the_code_the_tag_points_at(self):
        install = [s for s in self.steps if "GITHUB_ACTION_PATH" in s.get("run", "")]
        self.assertEqual(len(install), 1)
        self.assertIn(
            'install --disable-pip-version-check --quiet "$GITHUB_ACTION_PATH"',
            install[0]["run"],
        )

    def test_the_sample_workflow_uses_the_action_with_full_history(self):
        steps = load(".github/workflows/agentsmith.yml")["jobs"]["check"]["steps"]
        checkout = [
            s for s in steps if s.get("uses", "").startswith("actions/checkout")
        ]
        self.assertEqual(str(checkout[0]["with"]["fetch-depth"]), "0")
        self.assertIn("erickdronski/agentsmith@v0", [s.get("uses") for s in steps])


if __name__ == "__main__":
    unittest.main()
