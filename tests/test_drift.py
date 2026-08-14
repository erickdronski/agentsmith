"""Tests for drift detection.

Precision matters more than recall here. A checker that reports a false
contradiction gets removed from CI within a week, taking the true findings with
it — so roughly half of these tests assert that something is *not* reported.
"""

import unittest

from agentsmith.detectors import run_all
from agentsmith.drift import check, find_agent_file

from .fixtures import TS_SOURCE, FixtureRepo


def run_check(fixture, agent_file=None):
    repo = fixture.repo()
    return check(repo, run_all(repo), agent_file=agent_file)


def kinds(result, kind):
    return [d for d in result["drift"] if d["kind"] == kind]


class TestFileDiscovery(unittest.TestCase):
    def test_finds_agents_md_first(self):
        with FixtureRepo() as fixture:
            fixture.write("AGENTS.md", "# a")
            fixture.write("CLAUDE.md", "# c")
            self.assertEqual(find_agent_file(fixture.repo()), "AGENTS.md")

    def test_falls_back_to_claude_md(self):
        with FixtureRepo() as fixture:
            fixture.write("CLAUDE.md", "# c")
            self.assertEqual(find_agent_file(fixture.repo()), "CLAUDE.md")

    def test_reports_when_there_is_nothing_to_check(self):
        with FixtureRepo() as fixture:
            fixture.write("README.md", "# r")
            result = run_check(fixture)
            self.assertFalse(result["checked"])
            self.assertIn("No instruction file", result["message"])


class TestPackageManagerContradictions(unittest.TestCase):
    def test_npm_instructions_against_a_pnpm_lockfile(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            fixture.write("AGENTS.md", "Run `npm install` to set up.")
            result = run_check(fixture)
            contradictions = kinds(result, "contradiction")
            self.assertEqual(len(contradictions), 1)
            self.assertEqual(contradictions[0]["severity"], "error")
            self.assertIn("pnpm", contradictions[0]["suggestion"])

    def test_matching_instructions_produce_no_contradiction(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            fixture.write("AGENTS.md", "Run `pnpm install` to set up.")
            self.assertEqual(kinds(run_check(fixture), "contradiction"), [])

    def test_bare_word_yarn_is_not_enough_to_flag(self):
        """ "yarn" appears in prose constantly. Require a real subcommand."""
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            fixture.write(
                "AGENTS.md", "We migrated off yarn last year. Use `pnpm install`."
            )
            self.assertEqual(kinds(run_check(fixture), "contradiction"), [])

    def test_no_lockfile_means_no_opinion(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("AGENTS.md", "Run `npm install`.")
            self.assertEqual(kinds(run_check(fixture), "contradiction"), [])


class TestStaleScripts(unittest.TestCase):
    def test_missing_script_is_an_error(self):
        with FixtureRepo() as fixture:
            fixture.write_json(
                "package.json", {"name": "x", "scripts": {"test": "vitest"}}
            )
            fixture.write("AGENTS.md", "Run `npm run verify` before pushing.")
            errors = [
                d
                for d in run_check(fixture)["drift"]
                if d["kind"] == "stale" and d["severity"] == "error"
            ]
            self.assertTrue(errors)
            self.assertIn("verify", errors[0]["message"])

    def test_existing_script_is_fine(self):
        with FixtureRepo() as fixture:
            fixture.write_json(
                "package.json", {"name": "x", "scripts": {"test": "vitest"}}
            )
            fixture.write("AGENTS.md", "Run `npm run test`.")
            self.assertFalse(
                [
                    d
                    for d in run_check(fixture)["drift"]
                    if "no such script" in d["message"]
                ]
            )


class TestStalePaths(unittest.TestCase):
    def test_missing_repo_rooted_path_is_reported(self):
        with FixtureRepo() as fixture:
            fixture.write("src/index.ts", TS_SOURCE)
            fixture.write("AGENTS.md", "Entry point is `src/main.ts`.")
            stale = [
                d for d in run_check(fixture)["drift"] if "src/main.ts" in d["message"]
            ]
            self.assertTrue(stale)

    def test_existing_path_is_not_reported(self):
        with FixtureRepo() as fixture:
            fixture.write("src/index.ts", TS_SOURCE)
            fixture.write("AGENTS.md", "Entry point is `src/index.ts`.")
            self.assertFalse(
                [d for d in run_check(fixture)["drift"] if d["kind"] == "stale"]
            )

    def test_sibling_repository_references_are_not_stale(self):
        """The false positive that would get this tool uninstalled.

        Instruction files legitimately point at sibling repos and deploy
        targets. Those are not paths in this repository and must not be
        reported as missing.
        """
        with FixtureRepo() as fixture:
            fixture.write("src/index.ts", TS_SOURCE)
            fixture.write(
                "AGENTS.md",
                "The landing site lives in `other-repo/` and shares `../shared/lib.ts`.",
            )
            self.assertFalse(
                [d for d in run_check(fixture)["drift"] if d["kind"] == "stale"]
            )

    def test_prose_in_backticks_is_not_treated_as_a_path(self):
        with FixtureRepo() as fixture:
            fixture.write("src/index.ts", TS_SOURCE)
            fixture.write(
                "AGENTS.md",
                "Use `Node.js` and `TypeScript`. Prefer `async/await` over "
                "`.then()`. Call `useState` not `this.state`.",
            )
            self.assertFalse(
                [d for d in run_check(fixture)["drift"] if d["kind"] == "stale"]
            )


class TestUndocumented(unittest.TestCase):
    def test_missing_topic_is_informational_only(self):
        with FixtureRepo() as fixture:
            fixture.write("supabase/migrations/001.sql", "select 1;")
            fixture.write("AGENTS.md", "Be careful.")
            infos = kinds(run_check(fixture), "undocumented")
            self.assertTrue(infos)
            self.assertTrue(all(d["severity"] == "info" for d in infos))

    def test_documented_topic_is_not_reported(self):
        with FixtureRepo() as fixture:
            fixture.write("supabase/migrations/001.sql", "select 1;")
            fixture.write(
                "AGENTS.md",
                "Never edit applied migrations — they are generated and immutable.",
            )
            messages = " ".join(
                d["message"] for d in kinds(run_check(fixture), "undocumented")
            )
            self.assertNotIn("hand-edited", messages)

    def test_mention_inside_a_code_fence_does_not_count_as_documentation(self):
        with FixtureRepo() as fixture:
            fixture.write("supabase/migrations/001.sql", "select 1;")
            fixture.write(
                "AGENTS.md",
                "Setup:\n\n```bash\ncd migrations && ls generated\n```\n",
            )
            infos = kinds(run_check(fixture), "undocumented")
            self.assertTrue(
                any("hand-edited" in d["message"] for d in infos),
                "a mention inside a fence should not count as prose",
            )


class TestSeverityAccounting(unittest.TestCase):
    def test_counts_are_reported_per_severity(self):
        with FixtureRepo() as fixture:
            fixture.write_json(
                "package.json", {"name": "x", "scripts": {"test": "vitest"}}
            )
            fixture.write("pnpm-lock.yaml", "")
            fixture.write("src/index.ts", TS_SOURCE)
            fixture.write(
                "AGENTS.md",
                "Run `npm install`, then `npm run verify`. See `src/main.ts`.",
            )
            result = run_check(fixture)
            self.assertGreaterEqual(result["errors"], 2)
            self.assertGreaterEqual(result["warnings"], 1)

    def test_clean_file_reports_no_drift(self):
        with FixtureRepo() as fixture:
            fixture.write_json(
                "package.json", {"name": "x", "scripts": {"test": "vitest"}}
            )
            fixture.write("pnpm-lock.yaml", "")
            fixture.write(
                "AGENTS.md",
                "Install with `pnpm install`. Run tests with `pnpm run test`. "
                "CI runs the same. Never hand-edit generated files.",
            )
            result = run_check(fixture)
            self.assertEqual(result["errors"], 0)


if __name__ == "__main__":
    unittest.main()
