"""Tests for --target: one set of rules, written where each agent reads them.

The failure mode is the same as merge mode's — destroying somebody's writing —
multiplied by four files. So every target is tested for preservation, for
refusing rather than guessing, and for writing nothing at all when any one
target cannot be written safely.
"""

import os
import unittest

from agentsmith.drift import imports
from agentsmith.merge import BEGIN_MARKER, END_MARKER
from agentsmith.targets import parse_targets

from .fixtures import FixtureRepo
from .test_cli import run_cli

HANDWRITTEN_CLAUDE = """# CLAUDE.md

Never run the migration scripts against production from a laptop.
"""


class TargetCase(unittest.TestCase):
    def setUp(self):
        self.fixture = FixtureRepo()
        self.addCleanup(self.fixture.cleanup)
        self.fixture.write_json(
            "package.json", {"name": "x", "scripts": {"lint": "eslint ."}}
        )
        self.fixture.write("pnpm-lock.yaml", "")
        self.fixture.write("eslint.config.mjs", "export default [];")

    def path(self, relative):
        return os.path.join(self.fixture.root, relative)

    def read(self, relative):
        with open(self.path(relative), encoding="utf-8") as handle:
            return handle.read()

    def run_targets(self, *values, dry_run=False):
        args = [self.fixture.root]
        for value in values:
            args += ["--target", value]
        if dry_run:
            args.append("--dry-run")
        return run_cli(*args)


class TestParsing(unittest.TestCase):
    def test_comma_separated_and_repeated_values_combine(self):
        self.assertEqual(
            parse_targets(["copilot,claude", "agents", "claude"]),
            ["agents", "claude", "copilot"],
        )

    def test_unknown_values_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_targets(["agents,emacs"])


class TestAgentsTarget(TargetCase):
    def test_creates_a_managed_agents_md(self):
        code, _, err = self.run_targets("agents")
        self.assertEqual(code, 0)
        text = self.read("AGENTS.md")
        self.assertTrue(text.startswith(BEGIN_MARKER))
        self.assertIn("# AGENTS.md", text)
        self.assertIn("pnpm", text)
        self.assertIn("created AGENTS.md", err)

    def test_preserves_a_handwritten_agents_md(self):
        self.fixture.write("AGENTS.md", "# AGENTS.md\n\nAsk before touching billing.\n")
        self.run_targets("agents")
        text = self.read("AGENTS.md")
        self.assertTrue(text.startswith("# AGENTS.md\n\nAsk before touching billing."))
        self.assertEqual(text.count("# AGENTS.md"), 1)


class TestClaudeTarget(TargetCase):
    def test_new_claude_md_imports_rather_than_duplicates(self):
        self.run_targets("agents,claude")
        text = self.read("CLAUDE.md")
        self.assertEqual(imports(text), ["AGENTS.md"])
        self.assertNotIn("pnpm", text, "rules were duplicated instead of imported")
        self.assertTrue(text.startswith(BEGIN_MARKER))

    def test_imports_an_existing_agents_md_without_rewriting_it(self):
        self.fixture.write("AGENTS.md", "# Ours\n\nHand-written.\n")
        self.run_targets("claude")
        self.assertEqual(self.read("AGENTS.md"), "# Ours\n\nHand-written.\n")
        self.assertEqual(imports(self.read("CLAUDE.md")), ["AGENTS.md"])

    def test_without_agents_md_the_rules_go_into_claude_md(self):
        """Importing a file that will not exist would be a broken instruction."""
        code, _, err = self.run_targets("claude")
        self.assertEqual(code, 0)
        text = self.read("CLAUDE.md")
        self.assertIn("pnpm", text)
        self.assertEqual(imports(text), [])
        self.assertFalse(os.path.exists(self.path("AGENTS.md")))
        self.assertIn("--target agents", err)

    def test_handwritten_claude_md_is_preserved_and_gains_the_import(self):
        self.fixture.write("CLAUDE.md", HANDWRITTEN_CLAUDE)
        self.run_targets("agents,claude")
        text = self.read("CLAUDE.md")
        self.assertTrue(text.startswith(HANDWRITTEN_CLAUDE.rstrip("\n")))
        self.assertIn(BEGIN_MARKER, text)
        self.assertIn("@AGENTS.md", text)
        self.assertEqual(text.count("# CLAUDE.md"), 1)

    def test_claude_md_that_already_imports_is_left_alone(self):
        self.fixture.write("AGENTS.md", "# A\n")
        self.fixture.write("CLAUDE.md", "Read this first:\n\n@AGENTS.md\n")
        before = self.read("CLAUDE.md")
        _code, _out, err = self.run_targets("claude")
        self.assertEqual(self.read("CLAUDE.md"), before)
        self.assertIn("already imports AGENTS.md", err)

    def test_malformed_markers_refuse_and_write_nothing_anywhere(self):
        """One unsafe target stops every target, not just itself."""
        self.fixture.write("CLAUDE.md", "%s\nhalf a block\n" % BEGIN_MARKER)
        before = self.read("CLAUDE.md")
        code, _out, err = self.run_targets("agents,claude,copilot")
        self.assertEqual(code, 2)
        self.assertEqual(self.read("CLAUDE.md"), before)
        self.assertFalse(os.path.exists(self.path("AGENTS.md")))
        self.assertFalse(os.path.exists(self.path(".github/copilot-instructions.md")))
        self.assertIn("nothing was written", err)


class TestCursorTarget(TargetCase):
    def test_mdc_has_valid_frontmatter_first(self):
        self.run_targets("cursor")
        text = self.read(".cursor/rules/agentsmith.mdc")
        lines = text.splitlines()
        self.assertEqual(lines[0], "---")
        closing = lines.index("---", 1)
        frontmatter = lines[1:closing]
        self.assertTrue(any(line.startswith("description: ") for line in frontmatter))
        self.assertIn("alwaysApply: true", frontmatter)
        self.assertIn(BEGIN_MARKER, text)
        self.assertIn("ESLint (linter)", text)
        self.assertNotIn("\n# ", text, "an .mdc rule does not need an H1")

    def test_hand_edits_outside_the_block_survive(self):
        self.run_targets("cursor")
        edited = (
            self.read(".cursor/rules/agentsmith.mdc") + "\nAlso: prefer small PRs.\n"
        )
        self.fixture.write(".cursor/rules/agentsmith.mdc", edited)
        self.run_targets("cursor")
        text = self.read(".cursor/rules/agentsmith.mdc")
        self.assertIn("Also: prefer small PRs.", text)
        self.assertTrue(text.startswith("---\ndescription:"))

    def test_check_reads_the_mdc_file(self):
        self.run_targets("cursor")
        code, out, _ = run_cli(
            self.fixture.root, "--check", "--file", ".cursor/rules/agentsmith.mdc"
        )
        self.assertEqual(code, 0)
        self.assertIn("No drift found", out)


class TestCopilotTarget(TargetCase):
    def test_writes_copilot_instructions(self):
        self.run_targets("copilot")
        text = self.read(".github/copilot-instructions.md")
        self.assertIn("# Copilot instructions", text)
        self.assertIn("--check --file .github/copilot-instructions.md", text)
        self.assertTrue(text.rstrip().endswith(END_MARKER))


class TestAllTargets(TargetCase):
    def test_dry_run_shows_everything_and_writes_nothing(self):
        code, out, err = self.run_targets("agents,claude,cursor,copilot", dry_run=True)
        self.assertEqual(code, 0)
        for path in (
            "AGENTS.md",
            "CLAUDE.md",
            ".cursor/rules/agentsmith.mdc",
            ".github/copilot-instructions.md",
        ):
            self.assertFalse(os.path.exists(self.path(path)), path)
            self.assertIn("==> %s <==" % path, out)
            self.assertIn("%s: would create" % path, err)

    def test_second_run_changes_nothing(self):
        self.run_targets("agents,claude,cursor,copilot")
        first = {
            p: self.read(p)
            for p in (
                "AGENTS.md",
                "CLAUDE.md",
                ".cursor/rules/agentsmith.mdc",
                ".github/copilot-instructions.md",
            )
        }
        _code, _out, err = self.run_targets("agents,claude,cursor,copilot")
        for path, text in first.items():
            self.assertEqual(self.read(path), text, path)
        self.assertEqual(err.count("already up to date"), 4)

    def test_output_has_no_double_blank_lines(self):
        self.fixture.write("CLAUDE.md", HANDWRITTEN_CLAUDE)
        self.run_targets("agents,claude,cursor,copilot")
        for path in (
            "AGENTS.md",
            "CLAUDE.md",
            ".cursor/rules/agentsmith.mdc",
            ".github/copilot-instructions.md",
        ):
            self.assertNotIn("\n\n\n", self.read(path), path)

    def test_every_target_passes_its_own_check(self):
        self.run_targets("agents,claude,cursor,copilot")
        for path in (
            "AGENTS.md",
            "CLAUDE.md",
            ".cursor/rules/agentsmith.mdc",
            ".github/copilot-instructions.md",
        ):
            code, out, _ = run_cli(self.fixture.root, "--check", "--file", path)
            self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
