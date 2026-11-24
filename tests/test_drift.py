"""Tests for drift detection.

Precision matters more than recall here. A checker that reports a false
contradiction gets removed from CI within a week, taking the true findings with
it — so roughly half of these tests assert that something is *not* reported.
"""

import os
import unittest

from agentsmith.detectors import run_all
from agentsmith.drift import AGENT_FILES, check, find_agent_file

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

    def test_nothing_to_check_names_every_file_it_looked_for(self):
        """It named three of the seven, so a lone GEMINI.md that `--check`
        did find looked like a file it would never read."""
        with FixtureRepo() as fixture:
            message = run_check(fixture)["message"]
            for name in AGENT_FILES:
                self.assertIn(name, message)
            self.assertIsNone(run_check(fixture)["file"])


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
    def test_optional_and_workspace_invocations_do_not_require_root_scripts(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"scripts": {"test": "echo ok"}})
            fixture.write_json(
                "packages/web/package.json", {"scripts": {"build": "tsc"}}
            )
            fixture.write(
                "AGENTS.md",
                "\n".join(
                    "`%s`" % command
                    for command in (
                        "npm run --if-present lint",
                        "npm run lint --if-present",
                        "npm run --workspace web build",
                        "npm run build -w web",
                        "npm run build --workspace=web",
                        "npm run build --workspaces --if-present",
                        "npm run build --prefix packages/web",
                    )
                ),
            )
            self.assertEqual(kinds(run_check(fixture), "stale"), [])

    def test_script_arguments_and_adjacent_commands_do_not_make_a_script_optional(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"scripts": {"test": "echo ok"}})
            fixture.write(
                "AGENTS.md",
                "`npm run missing-args -- --if-present`\n"
                "`npm run missing-chain && npm run optional --if-present`\n"
                "`npm run missing-inline` and `npm run test -w web`\n"
                "`npm run missing-required --if-present=false`\n"
                "`pnpm run missing-pnpm --if-present`",
            )
            errors = [item["message"] for item in kinds(run_check(fixture), "stale")]
            names = {
                "missing-args",
                "missing-chain",
                "missing-inline",
                "missing-required",
                "missing-pnpm",
            }
            self.assertEqual(len(errors), len(names), errors)
            for name in names:
                self.assertTrue(any("`%s`" % name in message for message in errors))

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

    def test_script_is_checked_in_each_referenced_package(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"scripts": {"rootonly": "echo root"}})
            fixture.write_json(
                "packages/web/package.json", {"scripts": {"build": "tsc"}}
            )
            fixture.write_json(
                "packages/api/package.json", {"scripts": {"test": "vitest"}}
            )
            fixture.write(
                "AGENTS.md",
                (
                    "`npm run build` (in `packages/web/`), then `npm run build`.\n"
                    "`npm run build && npm run rootonly` (in `packages/api/`).\n"
                ),
            )
            errors = [
                item["message"]
                for item in kinds(run_check(fixture), "stale")
                if item["severity"] == "error"
            ]
            self.assertEqual(len(errors), 3, errors)
            self.assertTrue(
                any("build" in m and "but package.json" in m for m in errors)
            )
            for name in ("build", "rootonly"):
                self.assertTrue(
                    any(name in m and "packages/api/package.json" in m for m in errors)
                )

    def test_subdirectory_of_workspace_uses_its_nearest_manifest(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"scripts": {"test": "echo root"}})
            fixture.write_json(
                "apps/.frontend/package.json", {"scripts": {"build": "vite build"}}
            )
            fixture.write("apps/.frontend/src/index.ts", "export {};")
            fixture.write(
                "AGENTS.md", "`npm run build -- --mode=dev` (in `apps/.frontend/src/`)"
            )
            self.assertEqual(kinds(run_check(fixture), "stale"), [])

    def test_manifest_without_scripts_still_owns_its_workspace(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"scripts": {"build": "tsc"}})
            fixture.write_json("packages/web/package.json", {"name": "web"})
            fixture.write("AGENTS.md", "`npm run build` (in `packages/web/`)")
            errors = [
                d
                for d in kinds(run_check(fixture), "stale")
                if d["severity"] == "error"
            ]
            self.assertEqual(len(errors), 1, errors)
            self.assertIn("packages/web/package.json", errors[0]["message"])
            self.assertIn("(none)", errors[0]["suggestion"])


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


class TestStaleReferenceResolution(unittest.TestCase):
    """Two false positives from real repositories, pinned here.

    "then a plain `install.sh`" named a file in `automation/`, and
    `data/properties/<slug>/facts.toml` named a ledger that exists once per
    property. Both were reported as missing.
    """

    def stale(self, fixture):
        return [
            d["message"] for d in run_check(fixture)["drift"] if d["kind"] == "stale"
        ]

    def test_bare_filename_found_in_a_subdirectory(self):
        with FixtureRepo() as fixture:
            fixture.write("automation/install.sh", "#!/bin/sh\n")
            fixture.write("AGENTS.md", "Deploy, then run a plain `install.sh`.")
            self.assertEqual(self.stale(fixture), [])

    def test_bare_filename_found_in_a_tracked_tooling_directory(self):
        with FixtureRepo(git=True) as fixture:
            fixture.write(".cursor/install.sh", "#!/bin/sh\n")
            fixture.write("AGENTS.md", "Run `install.sh` once.")
            self.assertEqual(self.stale(fixture), [])

    def test_bare_filename_missing_everywhere_is_still_reported(self):
        with FixtureRepo() as fixture:
            fixture.write("automation/setup.sh", "#!/bin/sh\n")
            fixture.write("AGENTS.md", "Run `install.sh` once.")
            self.assertTrue(any("install.sh" in m for m in self.stale(fixture)))

    def test_bare_filename_only_in_vendored_code_is_reported(self):
        """A dependency's file is not the project's file."""
        with FixtureRepo() as fixture:
            fixture.write("node_modules/pkg/install.sh", "#!/bin/sh\n")
            fixture.write("src/index.ts", TS_SOURCE)
            fixture.write("AGENTS.md", "Run `install.sh` once.")
            self.assertTrue(any("install.sh" in m for m in self.stale(fixture)))

    def test_bare_filename_only_in_ignored_files_is_reported(self):
        with FixtureRepo(git=True) as fixture:
            fixture.write(".gitignore", "build-output/\n")
            fixture.write("build-output/install.sh", "#!/bin/sh\n")
            fixture.write("AGENTS.md", "Run `install.sh` once.")
            self.assertTrue(any("install.sh" in m for m in self.stale(fixture)))

    def test_angle_bracket_placeholder_matches_as_a_glob(self):
        with FixtureRepo() as fixture:
            fixture.write("data/properties/hotel-a/facts.toml", "")
            fixture.write("data/properties/hotel-b/facts.toml", "")
            fixture.write(
                "AGENTS.md",
                "Claims live in `data/properties/<slug>/facts.toml`; every claim "
                "needs a `facts.toml` entry.",
            )
            self.assertEqual(self.stale(fixture), [])

    def test_other_placeholder_spellings(self):
        with FixtureRepo() as fixture:
            fixture.write("docs/adr/0001-record-decisions.md", "")
            fixture.write("src/features/search/index.ts", TS_SOURCE)
            fixture.write("supabase/migrations/0007_add_users.sql", "")
            fixture.write(
                "AGENTS.md",
                "Write ADRs as `docs/adr/NNNN-title.md`. Features export from "
                "`src/features/{feature}/index.ts`, so `src/**/index.ts` is the "
                "entry pattern; see `src/.../index.ts`. Migrations are "
                "`supabase/migrations/*.sql`.",
            )
            self.assertEqual(self.stale(fixture), [])

    def test_placeholder_path_matching_nothing_is_reported(self):
        with FixtureRepo() as fixture:
            fixture.write("data/ledger/hotel-a.toml", "")
            fixture.write(
                "AGENTS.md", "Claims live in `data/properties/<slug>/facts.toml`."
            )
            self.assertEqual(self.stale(fixture), [])  # `data/properties` is gone too
            fixture.write("data/properties/README.md", "")
            self.assertTrue(
                any(
                    "data/properties/<slug>/facts.toml" in m
                    for m in self.stale(fixture)
                )
            )

    def test_placeholder_rooted_nowhere_is_not_reported(self):
        """`<your-repo>/x` and a gitignored `dist/*` are not checkable here."""
        with FixtureRepo(git=True) as fixture:
            fixture.write(".gitignore", "dist/\n")
            fixture.write("dist/bundle.js", "")
            fixture.write("src/index.ts", TS_SOURCE)
            fixture.write(
                "AGENTS.md", "Clone into `<your-repo>/agents/`. Ignore `dist/*`."
            )
            self.assertEqual(self.stale(fixture), [])

    def test_bare_glob_is_a_naming_convention_not_a_reference(self):
        with FixtureRepo() as fixture:
            fixture.write("src/index.ts", TS_SOURCE)
            fixture.write("AGENTS.md", "Name tests `*.test.ts`.")
            self.assertEqual(self.stale(fixture), [])

    def test_literal_bracket_route_is_checked_literally(self):
        """Next.js and expo-router use real `[id]` names. They must not be
        read as wildcards first, or a renamed route would be hidden."""
        with FixtureRepo() as fixture:
            fixture.write("src/app/product/[id].tsx", TS_SOURCE)
            fixture.write(
                "AGENTS.md", "The product screen is `src/app/product/[id].tsx`."
            )
            self.assertEqual(self.stale(fixture), [])

    def test_missing_bracket_route_is_reported(self):
        with FixtureRepo() as fixture:
            fixture.write("src/app/index.tsx", TS_SOURCE)
            fixture.write(
                "AGENTS.md", "The product screen is `src/app/product/[id].tsx`."
            )
            self.assertTrue(any("[id].tsx" in m for m in self.stale(fixture)))

    def test_bracket_used_as_a_template_falls_back_to_a_glob(self):
        with FixtureRepo() as fixture:
            fixture.write("docs/topics/billing.md", "")
            fixture.write("AGENTS.md", "One page per topic: `docs/topics/[topic].md`.")
            self.assertEqual(self.stale(fixture), [])

    def test_genuinely_missing_literal_path_is_still_reported(self):
        with FixtureRepo() as fixture:
            fixture.write("scripts/deploy.sh", "")
            fixture.write("AGENTS.md", "Run `scripts/release.sh`.")
            self.assertTrue(any("scripts/release.sh" in m for m in self.stale(fixture)))


class TestGeneratedOutputChecksClean(unittest.TestCase):
    """A file this tool generates must pass this tool's own check.

    It did not: "Test files are named `test_name.py`" was reported as a
    reference to a missing file, in every pytest repository, on a file
    generated seconds earlier.
    """

    def test_freshly_generated_file_has_no_drift(self):
        from agentsmith.render import render_markdown

        with FixtureRepo(git=True) as fixture:
            fixture.write_many(
                ["tests/test_%s.py" % n for n in "abcdefgh"], "import unittest\n"
            )
            fixture.write("pyproject.toml", "[tool.ruff]\nline-length = 100\n")
            for i in range(12):
                fixture.commit("Add feature %d" % i, touch="src/feature_%d.py" % i)
            repo = fixture.repo()
            findings = run_all(repo)
            generated = render_markdown(findings, "repo")
            self.assertIn("Test files are named", generated)
            fixture.write("AGENTS.md", generated)
            result = check(fixture.repo(), findings)
            self.assertEqual(result["drift"], [], result["drift"])


class TestImportsAndFrontmatter(unittest.TestCase):
    def test_claude_md_that_only_imports_is_checked_through_the_import(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            fixture.write("AGENTS.md", "Run `npm install`. See `src/missing.ts`.")
            fixture.write("src/index.ts", TS_SOURCE)
            fixture.write("CLAUDE.md", "@AGENTS.md\n")
            result = run_check(fixture, agent_file="CLAUDE.md")
            self.assertEqual(result["imports"], ["AGENTS.md"])
            self.assertEqual(result["errors"], 1)
            self.assertTrue(
                any(
                    "AGENTS.md references `src/missing.ts`" in d["message"]
                    for d in result["drift"]
                )
            )

    def test_import_satisfies_documentation_checks(self):
        """CLAUDE.md alone says nothing about tests; what Claude reads does."""
        with FixtureRepo() as fixture:
            fixture.write("supabase/migrations/001.sql", "select 1;")
            fixture.write("AGENTS.md", "Never edit applied migrations.")
            fixture.write("CLAUDE.md", "# CLAUDE.md\n\nSee:\n\n@AGENTS.md\n")
            result = run_check(fixture, agent_file="CLAUDE.md")
            self.assertEqual(kinds(result, "undocumented"), [])

    def test_import_of_a_missing_file_is_reported(self):
        with FixtureRepo() as fixture:
            fixture.write("CLAUDE.md", "@docs/conventions.md\n")
            result = run_check(fixture, agent_file="CLAUDE.md")
            self.assertTrue(
                any(
                    "imports `@docs/conventions.md`" in d["message"]
                    for d in result["drift"]
                )
            )

    def test_package_scopes_and_mentions_are_not_imports(self):
        with FixtureRepo() as fixture:
            fixture.write(
                "CLAUDE.md",
                "Ping @alice. We use @types/node and email ops@example.com.\n",
            )
            result = run_check(fixture, agent_file="CLAUDE.md")
            self.assertEqual(result["imports"], [])
            self.assertEqual(kinds(result, "stale"), [])

    def test_mdc_frontmatter_is_ignored(self):
        with FixtureRepo() as fixture:
            fixture.write("src/index.ts", TS_SOURCE)
            fixture.write(
                ".cursor/rules/x.mdc",
                "---\ndescription: Rules for `src/legacy.ts`\nglobs: src/**/*.ts\n"
                "alwaysApply: true\n---\n\nEntry point is `src/index.ts`.\n",
            )
            result = run_check(fixture, agent_file=".cursor/rules/x.mdc")
            self.assertTrue(result["checked"])
            self.assertEqual(kinds(result, "stale"), [])

    def test_mdc_frontmatter_saved_with_crlf_and_bom_is_ignored(self):
        # Written as bytes so the line endings are CRLF on every platform;
        # this is how a rule file saved by a Windows editor arrives.
        with FixtureRepo() as fixture:
            fixture.write("src/index.ts", TS_SOURCE)
            content = (
                "\ufeff---\r\ndescription: Rules for `src/legacy.ts`\r\n"
                "alwaysApply: true\r\n---\r\n\r\nEntry point is `src/index.ts`.\r\n"
            )
            path = os.path.join(fixture.root, ".cursor", "rules", "x.mdc")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(content.encode("utf-8"))
            result = run_check(fixture, agent_file=".cursor/rules/x.mdc")
            self.assertTrue(result["checked"])
            self.assertEqual(kinds(result, "stale"), [])

    def test_discovery_never_picks_a_directory(self):
        with FixtureRepo() as fixture:
            fixture.write(".cursor/rules/team.mdc", "---\nalwaysApply: true\n---\n")
            self.assertIsNone(find_agent_file(fixture.repo()))
            fixture.write(".cursor/rules/agentsmith.mdc", "rules")
            self.assertEqual(
                find_agent_file(fixture.repo()), ".cursor/rules/agentsmith.mdc"
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
