"""Tests for merge mode.

This is the feature that makes the tool adoptable, and its failure mode is
destroying somebody's writing — the most expensive thing this codebase can do.
So the tests are heavier on preservation than on the happy path, and several
assert that the tool *refuses* rather than guesses.
"""

import os
import unittest

from agentsmith.merge import (
    BEGIN_MARKER,
    END_MARKER,
    MergeError,
    has_markers,
    merge,
    preview,
    split_managed,
)

from .test_cli import run_cli
from .fixtures import FixtureRepo


HANDWRITTEN = """# AGENTS.md

## Architecture
The billing pipeline is eventually consistent. Never assume a write is
readable in the same request.

## Review notes
Ping @alice on anything touching auth.
"""


class TestSplit(unittest.TestCase):
    def test_no_markers(self):
        before, managed, after = split_managed("hello")
        self.assertEqual(before, "hello")
        self.assertIsNone(managed)
        self.assertEqual(after, "")

    def test_finds_the_block(self):
        text = "a\n%s\nX\n%s\nb" % (BEGIN_MARKER, END_MARKER)
        before, managed, after = split_managed(text)
        self.assertEqual(before, "a\n")
        self.assertIn("X", managed)
        self.assertEqual(after, "\nb")

    def test_duplicate_markers_refuse_rather_than_guess(self):
        """Guessing which text is managed risks deleting someone's writing."""
        text = "%s\nA\n%s\n%s\nB\n%s" % (
            BEGIN_MARKER,
            END_MARKER,
            BEGIN_MARKER,
            END_MARKER,
        )
        with self.assertRaises(MergeError):
            split_managed(text)

    def test_half_open_block_refuses(self):
        with self.assertRaises(MergeError):
            split_managed("%s\nA\n" % BEGIN_MARKER)

    def test_reversed_markers_refuse(self):
        with self.assertRaises(MergeError):
            split_managed("%s\nA\n%s" % (END_MARKER, BEGIN_MARKER))


class TestMergePreservesWriting(unittest.TestCase):
    def test_first_merge_keeps_every_existing_line(self):
        result = merge(HANDWRITTEN, "## Commands\n- Use pnpm.")
        for line in HANDWRITTEN.splitlines():
            if line.strip():
                self.assertIn(line, result, "lost a hand-written line: %r" % line)

    def test_first_merge_appends_rather_than_prepends(self):
        """The human's framing should still be the first thing a reader meets."""
        result = merge(HANDWRITTEN, "## Commands\n- Use pnpm.")
        self.assertLess(result.index("## Architecture"), result.index(BEGIN_MARKER))

    def test_second_merge_replaces_only_the_block(self):
        once = merge(HANDWRITTEN, "## Commands\n- Use npm.")
        twice = merge(once, "## Commands\n- Use pnpm.")
        self.assertIn("eventually consistent", twice)
        self.assertIn("@alice", twice)
        self.assertIn("pnpm", twice)
        self.assertNotIn("Use npm.", twice)

    def test_content_added_after_the_block_survives(self):
        once = merge(HANDWRITTEN, "## Commands\n- Use pnpm.")
        edited = once + "\n## Later\nAdded after the fact.\n"
        again = merge(edited, "## Commands\n- Use bun.")
        self.assertIn("Added after the fact.", again)
        self.assertIn("eventually consistent", again)

    def test_merging_is_idempotent(self):
        once = merge(HANDWRITTEN, "## Commands\n- Use pnpm.")
        twice = merge(once, "## Commands\n- Use pnpm.")
        self.assertEqual(once, twice)

    def test_empty_file_gets_just_the_block(self):
        result = merge("", "## Commands\n- Use pnpm.")
        self.assertTrue(result.startswith(BEGIN_MARKER))
        self.assertIn("pnpm", result)

    def test_markers_are_always_present_after_merge(self):
        result = merge(HANDWRITTEN, "## Commands")
        self.assertTrue(has_markers(result))

    def test_nothing_outside_the_block_is_ever_removed(self):
        """The property that makes this safe to run on a schedule."""
        once = merge(HANDWRITTEN, "## A\n- one")
        for body in ("## B\n- two", "", "## C\n- three\n- four"):
            once = merge(once, body)
            self.assertIn("eventually consistent", once)
            self.assertIn("@alice", once)


class TestPreview(unittest.TestCase):
    def test_describes_a_first_merge(self):
        self.assertIn("preserve", preview(HANDWRITTEN, "## Commands"))

    def test_describes_a_replacement(self):
        once = merge(HANDWRITTEN, "## Commands\n- a")
        self.assertIn("replace", preview(once, "## Commands\n- b"))

    def test_reports_a_malformed_file_without_raising(self):
        self.assertIn("cannot merge", preview(BEGIN_MARKER, "## Commands"))


class TestMergeCLI(unittest.TestCase):
    def repo_with_handwritten_agents(self):
        fixture = FixtureRepo()
        fixture.write_json("package.json", {"name": "x"})
        fixture.write("pnpm-lock.yaml", "")
        fixture.write("AGENTS.md", HANDWRITTEN)
        self.addCleanup(fixture.cleanup)
        return fixture

    def read(self, fixture):
        with open(os.path.join(fixture.root, "AGENTS.md"), encoding="utf-8") as handle:
            return handle.read()

    def test_merge_preserves_and_adds(self):
        fixture = self.repo_with_handwritten_agents()
        target = os.path.join(fixture.root, "AGENTS.md")
        code, _out, err = run_cli(fixture.root, "--out", target, "--merge")
        self.assertEqual(code, 0)
        text = self.read(fixture)
        self.assertIn("eventually consistent", text)
        self.assertIn("pnpm", text)
        self.assertIn("preserved", err)

    def test_merge_does_not_warn_about_overwriting(self):
        fixture = self.repo_with_handwritten_agents()
        target = os.path.join(fixture.root, "AGENTS.md")
        _code, _out, err = run_cli(fixture.root, "--out", target, "--merge")
        self.assertNotIn("being replaced", err)

    def test_without_merge_the_warning_still_fires(self):
        """The destructive path must keep telling the truth about itself."""
        fixture = self.repo_with_handwritten_agents()
        target = os.path.join(fixture.root, "AGENTS.md")
        _code, _out, err = run_cli(fixture.root, "--out", target)
        self.assertIn("being replaced", err)

    def test_dry_run_writes_nothing(self):
        fixture = self.repo_with_handwritten_agents()
        target = os.path.join(fixture.root, "AGENTS.md")
        before = self.read(fixture)
        code, _out, err = run_cli(fixture.root, "--out", target, "--merge", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.read(fixture), before)
        self.assertIn("would", err)

    def test_malformed_markers_exit_two_without_writing(self):
        fixture = self.repo_with_handwritten_agents()
        target = os.path.join(fixture.root, "AGENTS.md")
        fixture.write(
            "AGENTS.md",
            "%s\nA\n%s\n%s\nB\n%s"
            % (BEGIN_MARKER, END_MARKER, BEGIN_MARKER, END_MARKER),
        )
        before = self.read(fixture)
        code, _out, err = run_cli(fixture.root, "--out", target, "--merge")
        self.assertEqual(code, 2)
        self.assertEqual(self.read(fixture), before, "wrote despite refusing")
        self.assertIn("marker", err)

    def test_repeated_merges_stay_identical(self):
        fixture = self.repo_with_handwritten_agents()
        target = os.path.join(fixture.root, "AGENTS.md")
        run_cli(fixture.root, "--out", target, "--merge")
        first = self.read(fixture)
        run_cli(fixture.root, "--out", target, "--merge")
        self.assertEqual(first, self.read(fixture))


if __name__ == "__main__":
    unittest.main()
