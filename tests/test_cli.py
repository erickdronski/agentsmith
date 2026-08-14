"""Tests for rendering and the command line interface.

Exit codes get their own tests because this tool's main job in CI is to fail a
build at the right moment and not at the wrong one.
"""

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agentsmith.cli import main
from agentsmith.detectors import run_all
from agentsmith.evidence import Confidence, Evidence, Finding
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

    def test_json_shape(self):
        payload = json.loads(render_json([finding()], "repo", "0.1.0"))
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

    def test_bad_path_exits_two(self):
        code, _, err = run_cli("/nonexistent/path/xyz")
        self.assertEqual(code, 2)
        self.assertIn("error", err)


class TestCheckExitCodes(unittest.TestCase):
    def test_contradiction_fails_the_build(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            fixture.write("AGENTS.md", "Run `npm install`.")
            code, out, _ = run_cli(fixture.root, "--check")
            self.assertEqual(code, 1)
            self.assertIn("contradiction", out)

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

    def test_explicit_file_is_honored(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            fixture.write("docs/RULES.md", "Use `npm install`.")
            code, out, _ = run_cli(fixture.root, "--check", "--file", "docs/RULES.md")
            self.assertEqual(code, 1)
            self.assertIn("docs/RULES.md", out)

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
