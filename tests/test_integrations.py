"""Tests for the files other tools run: the GitHub Action and the pre-commit hook.

Neither runs in this suite — GitHub and pre-commit execute them — so these
pin the properties that would otherwise only fail in someone else's CI, and
parse the files with the project's own YAML reader rather than a dependency.
"""

import fnmatch
import os
import re
import unittest

from agentsmith.cli import build_parser
from agentsmith.miniyaml import parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(relative):
    with open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


def load(relative):
    return parse(read(relative))


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


class TestReleaseTags(unittest.TestCase):
    """`uses: erickdronski/agentsmith@v0` fails in every workflow until a
    `v0` tag exists, so each ref the documentation names must be one the
    release procedure creates."""

    DOCUMENTS = (
        "README.md",
        "CHANGELOG.md",
        "action.yml",
        ".github/workflows/agentsmith.yml",
        ".github/workflows/ci.yml",
    )

    def setUp(self):
        self.patterns = load(".github/workflows/release.yml")["on"]["push"]["tags"]
        self.procedures = {
            path: read(path)
            for path in (".github/workflows/release.yml", "CONTRIBUTING.md")
        }

    def starts_a_release(self, tag):
        # Tags here have no `/`, so fnmatch's `*` matches as GitHub's does.
        return any(fnmatch.fnmatchcase(tag, pattern) for pattern in self.patterns)

    def test_every_documented_ref_is_created_by_a_release(self):
        refs = set()
        for path in self.DOCUMENTS:
            found = re.findall(r"erickdronski/agentsmith@([\w.-]+)", read(path))
            refs.update(ref.rstrip(".") for ref in found)
        self.assertIn("v0", refs)
        for ref in sorted(refs):
            if re.fullmatch(r"v\d+\.\d+\.\d+", ref):
                continue  # the version tag each release pushes
            for path, procedure in self.procedures.items():
                self.assertIn("git tag -f %s '" % ref, procedure, path)
                self.assertIn("git push -f origin %s\n" % ref, procedure, path)

    def test_moving_the_major_tag_does_not_start_a_release(self):
        """Pushing `v0` again on every release must not build, and later
        upload to PyPI, the same version a second time."""
        for tag in ("v0.3.0", "v1.0.0", "v0.10.2"):
            self.assertTrue(self.starts_a_release(tag), tag)
        for tag in ("v0", "v1"):
            self.assertFalse(self.starts_a_release(tag), tag)


class TestPreCommitHook(unittest.TestCase):
    def setUp(self):
        hooks = load(".pre-commit-hooks.yaml")
        self.assertIsInstance(hooks, list)
        self.assertEqual(len(hooks), 1)
        self.hook = hooks[0]

    def test_declares_the_check_hook_with_the_required_keys(self):
        # pre-commit requires id, name, entry and language on every hook.
        for key in ("id", "name", "entry", "language"):
            self.assertTrue(self.hook.get(key), key)
        self.assertEqual(self.hook["id"], "agentsmith-check")
        self.assertEqual(self.hook["entry"], "agentsmith --check")
        self.assertEqual(self.hook["language"], "python")
        self.assertTrue(self.hook.get("description"))

    def test_checks_the_repository_not_the_staged_files(self):
        """The check takes a repository, not files: a staged filename would be
        read as the repository path. And drift does not depend on which files
        happen to be staged, so the hook runs on every commit."""
        self.assertEqual(str(self.hook["pass_filenames"]).lower(), "false")
        self.assertEqual(str(self.hook["always_run"]).lower(), "true")

    def test_entry_is_a_command_this_package_installs(self):
        """`language: python` pip-installs this repository and runs `entry`,
        so its first word must be a console script declared here."""
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as handle:
            pyproject = handle.read()
        command, *flags = self.hook["entry"].split()
        self.assertIn('\n%s = "agentsmith.cli:main"\n' % command, pyproject)
        build_parser().parse_args(flags)


if __name__ == "__main__":
    unittest.main()
