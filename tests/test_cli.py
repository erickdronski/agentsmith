"""Tests for rendering and the command line interface.

Exit codes get their own tests because this tool's main job in CI is to fail a
build at the right moment and not at the wrong one.
"""

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agentsmith.cli import build_parser, main
from agentsmith.detectors import run_all
from agentsmith.drift import AGENT_FILES
from agentsmith.evidence import Confidence, Evidence, Finding
from agentsmith.merge import BEGIN_MARKER
from agentsmith.render import GENERATED_MARKER, render_json, render_markdown

from .fixtures import FixtureRepo


def run_cli(*args):
    """Run the CLI, capturing output and exit code."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


def finding(
    key="k",
    section="Commands",
    rule="Do the thing.",
    confidence=Confidence.CERTAIN,
    evidence=None,
):
    return Finding(
        key=key,
        section=section,
        rule=rule,
        confidence=confidence,
        evidence=evidence if evidence is not None else [Evidence("src", "because")],
    )


class TestRender(unittest.TestCase):
    def test_marker_is_present(self):
        output = render_markdown([finding()], "repo")
        self.assertIn(GENERATED_MARKER, output)

    def test_sections_follow_the_declared_order(self):
        output = render_markdown(
            [
                finding(key="a", section="Do not edit"),
                finding(key="b", section="Commands"),
                finding(key="c", section="Stack"),
            ],
            "repo",
        )
        self.assertLess(output.index("## Stack"), output.index("## Commands"))
        self.assertLess(output.index("## Commands"), output.index("## Do not edit"))

    def test_certain_rules_carry_no_qualifier(self):
        # Check the body only — the footer legend explains the qualifier and
        # necessarily contains the phrase.
        body = render_markdown([finding()], "repo").split("---")[0]
        self.assertNotIn("seen in", body)

    def test_weak_rules_show_their_share(self):
        output = render_markdown(
            [
                finding(
                    confidence=Confidence.WEAK,
                    evidence=[Evidence("src", "d", observed=13, total=20)],
                )
            ],
            "repo",
        )
        self.assertIn("65%", output)

    def test_min_confidence_filters(self):
        findings = [
            finding(key="strong", confidence=Confidence.CERTAIN),
            finding(
                key="weak",
                rule="Maybe.",
                confidence=Confidence.WEAK,
                evidence=[Evidence("src", "d", observed=13, total=20)],
            ),
        ]
        output = render_markdown(findings, "repo", min_confidence=Confidence.STRONG)
        self.assertIn("Do the thing.", output)
        self.assertNotIn("Maybe.", output)

    def test_explain_includes_evidence(self):
        output = render_markdown(
            [finding(evidence=[Evidence("git log", "counted", 9, 10, ["a.ts"])])],
            "repo",
            explain=True,
        )
        self.assertIn("<details>", output)
        self.assertIn("git log", output)
        self.assertIn("a.ts", output)

    def test_empty_findings_says_so_rather_than_padding(self):
        """A generated file must never invent generic advice to look complete."""
        output = render_markdown([], "repo")
        self.assertIn("No conventions could be detected", output)
        self.assertNotIn("## Commands", output)

    def test_notes_are_rendered(self):
        output = render_markdown([finding(confidence=Confidence.LIKELY)], "repo")
        self.assertIn("Do the thing.", output)

    def test_no_double_blank_lines(self):
        """markdownlint MD012: generated output used to have two blank lines
        after every section, and before the footer."""
        output = render_markdown(
            [
                finding(key="a", section="Stack", rule="One."),
                finding(key="b", section="Commands", rule="Two:\n\n- `x`\n- `y`"),
                finding(key="c", section="Commands", rule="Three."),
            ],
            "repo",
            explain=True,
        )
        self.assertNotIn("\n\n\n", output)
        self.assertTrue(output.endswith(".\n") and not output.endswith("\n\n"))

    def test_footer_does_not_nest_emphasis(self):
        footer = render_markdown([finding()], "repo").rstrip().splitlines()[-1]
        self.assertTrue(footer.startswith("_1 rule derived from this repository._ "))

    def test_title_can_be_omitted_for_merging(self):
        """A block merged into someone's file must not add a second H1."""
        output = render_markdown([finding()], "repo", title=None)
        self.assertNotIn("# AGENTS.md", output)
        self.assertIn("every rule in this block", output)
        self.assertIn(GENERATED_MARKER, output)

    def test_json_shape(self):
        payload = json.loads(render_json([finding()], "repo", "0.2.0"))
        self.assertEqual(payload["tool"], "agentsmith")
        self.assertEqual(payload["finding_count"], 1)
        self.assertEqual(payload["findings"][0]["key"], "k")


class TestCLI(unittest.TestCase):
    def test_generates_to_stdout(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            code, out, _ = run_cli(fixture.root)
            self.assertEqual(code, 0)
            self.assertIn("pnpm", out)

    def test_writes_to_a_file(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            target = os.path.join(fixture.root, "AGENTS.md")
            code, _, err = run_cli(fixture.root, "--out", target)
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(target))
            with open(target, encoding="utf-8") as handle:
                self.assertIn(GENERATED_MARKER, handle.read())
            self.assertIn("wrote", err)

    def test_warns_before_replacing_a_handwritten_file(self):
        """Silently destroying someone's hand-written AGENTS.md is unacceptable."""
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            target = os.path.join(fixture.root, "AGENTS.md")
            fixture.write("AGENTS.md", "# Written by a human\n")
            _, _, err = run_cli(fixture.root, "--out", target)
            self.assertIn("not generated by agentsmith", err)

    def test_does_not_warn_when_replacing_its_own_output(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            target = os.path.join(fixture.root, "AGENTS.md")
            run_cli(fixture.root, "--out", target)
            _, _, err = run_cli(fixture.root, "--out", target)
            self.assertNotIn("not generated by agentsmith", err)

    def test_json_output(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            code, out, _ = run_cli(fixture.root, "--format", "json")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["tool"], "agentsmith")

    def test_skip_disables_a_detector(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            _, out, _ = run_cli(fixture.root, "--skip", "packaging")
            # The lockfile still appears under "do not edit" (that is the
            # layout detector, correctly), so assert the packaging rule
            # specifically is gone rather than the word "pnpm".
            self.assertNotIn("for all dependency operations", out)

    def test_title_follows_the_output_file(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            target = os.path.join(fixture.root, "CLAUDE.md")
            run_cli(fixture.root, "--out", target)
            with open(target, encoding="utf-8") as handle:
                self.assertTrue(handle.read().startswith("# CLAUDE.md\n"))

    def test_dry_run_without_merge_writes_nothing(self):
        """--dry-run was ignored without --merge, and the file was replaced."""
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            fixture.write("AGENTS.md", "# Written by a human\n")
            target = os.path.join(fixture.root, "AGENTS.md")
            code, out, err = run_cli(fixture.root, "--out", target, "--dry-run")
            self.assertEqual(code, 0)
            with open(target, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "# Written by a human\n")
            self.assertIn("would replace", err)
            self.assertIn("--merge", err)
            self.assertIn("pnpm", out)

    def test_target_and_out_are_mutually_exclusive(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            code, _, err = run_cli(fixture.root, "--target", "claude", "--out", "x.md")
            self.assertEqual(code, 2)
            self.assertIn("cannot be combined", err)
            self.assertFalse(os.path.exists(os.path.join(fixture.root, "CLAUDE.md")))

    def test_unknown_target_is_rejected(self):
        with FixtureRepo() as fixture:
            code, _, err = run_cli(fixture.root, "--target", "agents,vim")
            self.assertEqual(code, 2)
            self.assertIn("unknown target 'vim'", err)
            self.assertFalse(os.path.exists(os.path.join(fixture.root, "AGENTS.md")))

    def test_target_rejects_json_and_check(self):
        with FixtureRepo() as fixture:
            self.assertEqual(
                run_cli(fixture.root, "--target", "agents", "--format", "json")[0], 2
            )
            self.assertEqual(
                run_cli(fixture.root, "--target", "agents", "--check")[0], 2
            )

    def test_bad_path_exits_two(self):
        code, _, err = run_cli("/nonexistent/path/xyz")
        self.assertEqual(code, 2)
        self.assertIn("error", err)


class TestOutPaths(unittest.TestCase):
    """`--out` into a directory that does not exist yet crashed with a
    FileNotFoundError traceback, and `--out` naming a directory with an
    IsADirectoryError. Missing directories are now created, as `--target`
    creates `.cursor/rules/`; an impossible path is one line and exit 2."""

    def setUp(self):
        self.fixture = FixtureRepo()
        self.addCleanup(self.fixture.cleanup)
        self.fixture.write_json("package.json", {"name": "x"})
        self.fixture.write("pnpm-lock.yaml", "")

    def path(self, *parts):
        return os.path.join(self.fixture.root, *parts)

    def in_repo(self):
        """Run from inside the fixture, as `agentsmith --out x` is typed."""
        previous = os.getcwd()
        os.chdir(self.fixture.root)
        self.addCleanup(os.chdir, previous)

    def assert_one_line_error(self, code, err, fragment):
        self.assertEqual(code, 2, err)
        self.assertIn(fragment, err)
        self.assertTrue(err.startswith("error: "), err)
        self.assertEqual(err.count("\n"), 1, err)
        self.assertNotIn("Traceback", err)

    def test_out_creates_missing_directories(self):
        self.in_repo()
        code, _, err = run_cli(".", "--out", "no/such/dir/AGENTS.md")
        self.assertEqual(code, 0, err)
        with open(self.path("no", "such", "dir", "AGENTS.md"), encoding="utf-8") as h:
            self.assertIn("pnpm", h.read())
        self.assertIn("wrote no/such/dir/AGENTS.md", err)

    def test_out_creates_a_cursor_rules_directory(self):
        self.in_repo()
        code, _, err = run_cli(".", "--out", ".cursor/rules/x.mdc")
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(self.path(".cursor", "rules", "x.mdc")))

    def test_merge_creates_missing_directories(self):
        target = self.path("docs", "agents", "AGENTS.md")
        code, _, err = run_cli(self.fixture.root, "--out", target, "--merge")
        self.assertEqual(code, 0, err)
        with open(target, encoding="utf-8") as handle:
            self.assertTrue(handle.read().startswith(BEGIN_MARKER))

    def test_out_outside_the_repository_creates_directories(self):
        with FixtureRepo() as elsewhere:
            target = os.path.join(elsewhere.root, "a", "b", "AGENTS.md")
            for extra in ([], ["--merge"]):
                code, _, err = run_cli(self.fixture.root, "--out", target, *extra)
                self.assertEqual(code, 0, err)
                self.assertTrue(os.path.isfile(target))

    def test_dry_run_names_the_directory_and_creates_nothing(self):
        target = self.path("no", "such", "AGENTS.md")
        for extra in ([], ["--merge"]):
            code, out, err = run_cli(
                self.fixture.root, "--out", target, "--dry-run", *extra
            )
            self.assertEqual(code, 0, err)
            self.assertIn("would create it, and the directory", err)
            self.assertIn("pnpm", out)
            self.assertFalse(os.path.exists(self.path("no")))

    def test_a_directory_exits_two_in_every_mode(self):
        for extra in ([], ["--merge"], ["--dry-run"], ["--merge", "--dry-run"]):
            code, out, err = run_cli(
                self.fixture.root, "--out", self.fixture.root, *extra
            )
            self.assert_one_line_error(code, err, "is a directory")
            self.assertIn("AGENTS.md", err, "the error should suggest a file name")
            self.assertEqual(out, "")

    def test_current_directory_and_trailing_separator_are_directories(self):
        self.in_repo()
        code, _, err = run_cli(".", "--out", ".")
        self.assert_one_line_error(code, err, "--out . is a directory")
        code, _, err = run_cli(".", "--out", "new" + os.sep)
        self.assert_one_line_error(code, err, "is a directory")
        self.assertFalse(os.path.exists(self.path("new")))

    def test_a_file_in_the_way_exits_two(self):
        target = self.path("package.json", "AGENTS.md")
        for extra in ([], ["--merge"], ["--dry-run"]):
            code, _, err = run_cli(self.fixture.root, "--out", target, *extra)
            self.assert_one_line_error(code, err, "is a file, not a directory")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions"
    )
    def test_an_unwritable_directory_exits_two(self):
        locked = self.path("locked")
        os.mkdir(locked)
        os.chmod(locked, 0o555)
        self.addCleanup(os.chmod, locked, 0o755)
        for target in (
            os.path.join(locked, "AGENTS.md"),
            os.path.join(locked, "sub", "AGENTS.md"),
        ):
            for extra in ([], ["--merge"], ["--dry-run"]):
                code, _, err = run_cli(self.fixture.root, "--out", target, *extra)
                self.assert_one_line_error(code, err, "is not writable")
        self.assertEqual(os.listdir(locked), [])

    def test_a_quoted_tilde_means_the_home_directory(self):
        """A ~ quoted in a Makefile or CI step is not expanded by a shell. It
        used to create a directory named ~ here while reporting `wrote ~/...`."""
        self.in_repo()
        with FixtureRepo() as home:
            environ = {"HOME": home.root, "USERPROFILE": home.root}
            with mock.patch.dict(os.environ, environ):
                expected = os.path.expanduser("~/probe/AGENTS.md")
                code, _, err = run_cli(".", "--out", "~/probe/AGENTS.md")
            self.assertEqual(code, 0, err)
            self.assertTrue(
                os.path.isfile(os.path.join(home.root, "probe", "AGENTS.md"))
            )
            self.assertIn("wrote %s" % expected, err)
        self.assertFalse(os.path.exists(self.path("~")))

    @unittest.skipIf(os.name == "nt", "Windows guesses ~user from USERPROFILE")
    def test_a_tilde_that_names_no_home_directory_exits_two(self):
        self.in_repo()
        out = "~agentsmith-no-such-user/AGENTS.md"
        for extra in ([], ["--merge"], ["--dry-run"]):
            code, _, err = run_cli(".", "--out", out, *extra)
            self.assert_one_line_error(code, err, "does not name a home directory")
            self.assertIn("./" + out, err)
        self.assertEqual(
            [n for n in os.listdir(self.fixture.root) if n.startswith("~")], []
        )

    def test_merge_dry_run_on_an_up_to_date_file_says_so(self):
        """It said it would replace the managed block, with line counts that
        differed by the two marker lines although nothing would change."""
        target = self.path("AGENTS.md")
        code, _, err = run_cli(self.fixture.root, "--out", target, "--merge")
        self.assertEqual(code, 0, err)
        with open(target, encoding="utf-8") as handle:
            written = handle.read()
        code, out, err = run_cli(
            self.fixture.root, "--out", target, "--merge", "--dry-run"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "%s: already up to date\n" % target)
        self.assertEqual(out, written)
        code, _, err = run_cli(self.fixture.root, "--out", target, "--merge")
        self.assertEqual((code, err), (0, "%s: already up to date\n" % target))
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), written)

    def test_a_failed_write_is_an_error_not_a_traceback(self):
        """A read-only filesystem or a race gets past the up-front checks."""
        denied = PermissionError(13, "Permission denied")
        with mock.patch("agentsmith.cli.open", side_effect=denied, create=True):
            for extra in ([], ["--merge"]):
                code, _, err = run_cli(
                    self.fixture.root, "--out", self.path("AGENTS.md"), *extra
                )
                self.assert_one_line_error(code, err, "Permission denied")


class TestCheckExitCodes(unittest.TestCase):
    def test_contradiction_fails_the_build(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            fixture.write("AGENTS.md", "Run `npm install`.")
            code, out, _ = run_cli(fixture.root, "--check")
            self.assertEqual(code, 1)
            self.assertIn("contradiction", out)

    def test_generated_workspace_commands_pass_until_the_workspace_script_changes(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"scripts": {"lint": "echo lint"}})
            fixture.write_json(
                "packages/web/package.json", {"scripts": {"build": "tsc"}}
            )
            fixture.write(
                ".github/workflows/ci.yml",
                (
                    "on: push\njobs:\n  verify:\n    runs-on: ubuntu-latest\n"
                    "    defaults:\n      run:\n        working-directory: packages/web\n"
                    "    steps:\n      - run: npm run build\n"
                ),
            )
            code, instructions, _ = run_cli(fixture.root)
            self.assertEqual(code, 0)
            self.assertIn("`npm run build` (in `packages/web/`)", instructions)
            fixture.write("AGENTS.md", instructions)
            self.assertEqual(run_cli(fixture.root, "--check")[0], 0)

            fixture.write_json(
                "packages/web/package.json", {"scripts": {"bundle": "tsc"}}
            )
            code, report, _ = run_cli(fixture.root, "--check")
            self.assertEqual(code, 1)
            self.assertIn("packages/web/package.json", report)
            self.assertIn("bundle", report)

    def test_workspace_path_preserves_a_leading_dot_without_a_root_manifest(self):
        with FixtureRepo() as fixture:
            fixture.write_json(".web/package.json", {"scripts": {"test": "vitest"}})
            fixture.write(
                ".github/workflows/ci.yml",
                (
                    "on: push\njobs:\n  verify:\n    runs-on: ubuntu-latest\n"
                    "    steps:\n      - run: npm run test\n        working-directory: ./.web\n"
                ),
            )
            code, instructions, _ = run_cli(fixture.root)
            self.assertEqual(code, 0)
            self.assertIn("`npm run test` (in `.web/`)", instructions)
            fixture.write("AGENTS.md", instructions)
            self.assertEqual(run_cli(fixture.root, "--check")[0], 0)
            fixture.write_json(".web/package.json", {"scripts": {}})
            self.assertEqual(run_cli(fixture.root, "--check")[0], 1)

    def test_clean_file_passes(self):
        with FixtureRepo() as fixture:
            fixture.write_json(
                "package.json", {"name": "x", "scripts": {"test": "vitest"}}
            )
            fixture.write("pnpm-lock.yaml", "")
            fixture.write(
                "AGENTS.md",
                "Install with `pnpm install`. Run `pnpm run test`. CI runs the "
                "same. Never hand-edit generated files. Commits use "
                "conventional format.",
            )
            code, _, _ = run_cli(fixture.root, "--check")
            self.assertEqual(code, 0)

    def test_informational_drift_alone_does_not_fail(self):
        """Undocumented conventions are advice, not build breakers."""
        with FixtureRepo() as fixture:
            fixture.write("supabase/migrations/001.sql", "select 1;")
            fixture.write("AGENTS.md", "Be careful out there.")
            code, out, _ = run_cli(fixture.root, "--check")
            self.assertEqual(code, 0)
            self.assertIn("undocumented", out)

    def test_strict_fails_on_informational_drift(self):
        with FixtureRepo() as fixture:
            fixture.write("supabase/migrations/001.sql", "select 1;")
            fixture.write("AGENTS.md", "Be careful out there.")
            code, _, _ = run_cli(fixture.root, "--check", "--strict")
            self.assertEqual(code, 1)

    def test_no_instruction_file_is_not_a_failure(self):
        with FixtureRepo() as fixture:
            fixture.write("README.md", "# r")
            code, out, _ = run_cli(fixture.root, "--check")
            self.assertEqual(code, 0)
            self.assertIn("No instruction file", out)

    def test_explicit_file_that_cannot_be_read_exits_two(self):
        """A typo in `--file` must not become a check that always passes."""
        with FixtureRepo() as fixture:
            fixture.write("AGENTS.md", "# Rules\n")
            code, out, _ = run_cli(fixture.root, "--check", "--file", "CLAUDE.MD")
            self.assertEqual(code, 2)
            self.assertIn("Could not read CLAUDE.MD", out)
            code, out, _ = run_cli(
                fixture.root, "--check", "--file", "CLAUDE.MD", "--format", "json"
            )
            self.assertEqual(code, 2)
            self.assertFalse(json.loads(out)["checked"])

    def test_found_file_that_cannot_be_read_exits_two(self):
        """Found without --file, an unreadable AGENTS.md exited 0 — so the
        action and the pre-commit hook passed it. Windows PowerShell 5.1's
        `>` writes UTF-16, which is how such a file usually arrives."""
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            with open(os.path.join(fixture.root, "AGENTS.md"), "wb") as handle:
                handle.write("Run `npm install` first.\n".encode("utf-16"))
            for extra in ([], ["--strict"]):
                code, out, _ = run_cli(fixture.root, "--check", *extra)
                self.assertEqual(code, 2, out)
                self.assertIn("Could not read AGENTS.md: it is not UTF-8 text", out)
            code, out, _ = run_cli(fixture.root, "--check", "--format", "json")
            self.assertEqual(code, 2)
            payload = json.loads(out)
            self.assertEqual(
                (payload["file"], payload["checked"]), ("AGENTS.md", False)
            )

    def test_import_that_cannot_be_read_exits_two(self):
        """It was reported as an import of a file that does not exist, a
        warning, so the check passed without reading the rules."""
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            with open(os.path.join(fixture.root, "AGENTS.md"), "wb") as handle:
                handle.write("Run `npm install` first.\n".encode("utf-16"))
            fixture.write("GEMINI.md", "# GEMINI.md\n\n@./AGENTS.md\n")
            code, out, _ = run_cli(fixture.root, "--check", "--file", "GEMINI.md")
            self.assertEqual(code, 2, out)
            self.assertIn("Could not read AGENTS.md, which GEMINI.md imports", out)
            self.assertNotIn("does not exist", out)

    def test_help_names_every_file_check_looks_for(self):
        # Wrapping can break a line after a hyphen; whitespace is irrelevant.
        text = "".join(build_parser().format_help().split())
        for name in AGENT_FILES:
            self.assertIn(name, text)

    def test_explicit_file_is_honored(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            fixture.write("docs/RULES.md", "Use `npm install`.")
            code, out, _ = run_cli(fixture.root, "--check", "--file", "docs/RULES.md")
            self.assertEqual(code, 1)
            self.assertIn("docs/RULES.md", out)

    def test_check_names_the_imported_file(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            fixture.write("AGENTS.md", "Run `npm install`.")
            fixture.write("CLAUDE.md", "@AGENTS.md\n")
            code, out, _ = run_cli(fixture.root, "--check", "--file", "CLAUDE.md")
            self.assertEqual(code, 1)
            self.assertIn("CLAUDE.md (and AGENTS.md, which it imports)", out)

    def test_check_json_output(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            fixture.write("AGENTS.md", "Run `npm install`.")
            _code, out, _ = run_cli(fixture.root, "--check", "--format", "json")
            payload = json.loads(out)
            self.assertEqual(payload["file"], "AGENTS.md")
            self.assertGreaterEqual(payload["errors"], 1)


class TestSelfAnalysis(unittest.TestCase):
    """agentsmith must produce sane output for its own repository."""

    def setUp(self):
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_runs_on_itself_without_error(self):
        code, out, _ = run_cli(self.root)
        self.assertEqual(code, 0)
        self.assertIn("AGENTS.md", out)

    def test_detects_its_own_test_suite(self):
        from agentsmith.repo import Repo

        findings = {f.key: f for f in run_all(Repo(self.root))}
        self.assertIn("test-framework", findings)
        self.assertIn("unittest", findings["test-framework"].rule)

    def test_reports_no_dependencies_or_lockfiles(self):
        from agentsmith.repo import Repo

        findings = {f.key: f for f in run_all(Repo(self.root))}
        self.assertNotIn("package-manager", findings)


if __name__ == "__main__":
    unittest.main()
