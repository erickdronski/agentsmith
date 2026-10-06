"""Tests for the detectors, against real repositories built on disk.

The recurring theme in these tests is **restraint**: a detector must find
nothing when there is nothing to find. Half of what follows asserts silence,
because the failure mode that would make this tool useless is not missing a
convention — it is confidently inventing one from four files.
"""

import os
import unittest

from agentsmith.detectors import (
    history,
    layout,
    packaging,
    run_all,
    style,
    testing,
    verification,
)
from agentsmith.evidence import Confidence, dominant
from agentsmith.repo import Repo

from .fixtures import PY_SOURCE, TS_SOURCE, TS_SOURCE_NO_SEMI, FixtureRepo, keys


class TestDominant(unittest.TestCase):
    """The gate that decides whether anything gets asserted at all."""

    def test_small_samples_produce_nothing(self):
        self.assertIsNone(dominant({"a": 3, "b": 1}))

    def test_split_populations_produce_nothing(self):
        """55/45 is not a convention. It is two conventions."""
        self.assertIsNone(dominant({"a": 55, "b": 45}))

    def test_clear_majority_is_reported(self):
        result = dominant({"a": 90, "b": 10})
        self.assertIsNotNone(result)
        self.assertEqual(result["value"], "a")
        self.assertEqual(result["confidence"], Confidence.STRONG)

    def test_moderate_majority_is_downgraded(self):
        result = dominant({"a": 75, "b": 25})
        self.assertEqual(result["confidence"], Confidence.LIKELY)

    def test_bare_majority_is_marked_weak(self):
        result = dominant({"a": 65, "b": 35})
        self.assertEqual(result["confidence"], Confidence.WEAK)

    def test_runners_up_are_retained(self):
        result = dominant({"a": 90, "b": 6, "c": 4})
        self.assertEqual(result["runners_up"][0][0], "b")


class TestPackaging(unittest.TestCase):
    def test_lockfile_determines_package_manager(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            found = keys(packaging.detect(fixture.repo()))
            self.assertIn("package-manager", found)
            self.assertIn("pnpm", found["package-manager"].rule)

    def test_two_lockfiles_is_reported_not_guessed(self):
        with FixtureRepo() as fixture:
            fixture.write_json("package.json", {"name": "x"})
            fixture.write("pnpm-lock.yaml", "")
            fixture.write("package-lock.json", "{}")
            found = keys(packaging.detect(fixture.repo()))
            self.assertIn("package-manager-conflict", found)
            self.assertNotIn("package-manager", found)

    def test_packagemanager_field_disagreeing_with_lockfile_is_flagged(self):
        with FixtureRepo() as fixture:
            fixture.write_json(
                "package.json", {"name": "x", "packageManager": "yarn@4.0.0"}
            )
            fixture.write("pnpm-lock.yaml", "")
            found = keys(packaging.detect(fixture.repo()))
            self.assertIn("disagrees", found["package-manager"].rule)

    def test_scripts_are_surfaced_with_the_right_runner(self):
        with FixtureRepo() as fixture:
            fixture.write_json(
                "package.json",
                {"name": "x", "scripts": {"test": "vitest", "build": "tsc"}},
            )
            fixture.write("pnpm-lock.yaml", "")
            found = keys(packaging.detect(fixture.repo()))
            self.assertIn("pnpm run test", found["scripts"].rule)

    def test_monorepo_warns_about_install_location(self):
        with FixtureRepo() as fixture:
            fixture.write_json(
                "package.json", {"name": "x", "workspaces": ["packages/*"]}
            )
            found = keys(packaging.detect(fixture.repo()))
            self.assertIn("workspaces", found)
            self.assertIn("root", found["workspaces"].rule)

    def test_non_javascript_repo_produces_nothing(self):
        with FixtureRepo() as fixture:
            fixture.write("README.md", "# hi")
            self.assertEqual(packaging.detect(fixture.repo()), [])

    def test_other_ecosystems_are_recognized(self):
        with FixtureRepo() as fixture:
            fixture.write("Cargo.toml", "[package]")
            found = keys(packaging.detect(fixture.repo()))
            self.assertIn("ecosystem-Cargo.toml", found)


class TestTesting(unittest.TestCase):
    def test_framework_from_config_file(self):
        with FixtureRepo() as fixture:
            fixture.write("vitest.config.ts", "export default {}")
            fixture.write_many(
                ["src/a.test.ts", "src/b.test.ts", "src/c.test.ts"], TS_SOURCE
            )
            found = keys(testing.detect(fixture.repo()))
            self.assertIn("vitest", found["test-framework"].rule)

    def test_location_and_naming_are_derived_from_the_corpus(self):
        with FixtureRepo() as fixture:
            fixture.write_many(
                [
                    "tests/test_a.py",
                    "tests/test_b.py",
                    "tests/test_c.py",
                    "tests/test_d.py",
                    "tests/test_e.py",
                ],
                PY_SOURCE,
            )
            found = keys(testing.detect(fixture.repo()))
            self.assertIn("top-level `tests/`", found["test-location"].rule)
            self.assertIn("test_name.py", found["test-naming"].rule)

    def test_colocated_tests_are_recognized(self):
        with FixtureRepo() as fixture:
            fixture.write_many(
                [
                    "src/a.test.ts",
                    "src/b.test.ts",
                    "src/c.test.ts",
                    "src/d.test.ts",
                    "src/e.test.ts",
                ],
                TS_SOURCE,
            )
            found = keys(testing.detect(fixture.repo()))
            self.assertIn("co-located", found["test-location"].rule)

    def test_one_python_file_does_not_name_the_runner(self):
        """A Node project with one Python helper test is not a unittest project."""
        node_test = "import test from 'node:test';\ntest('x', () => {});\n"
        with FixtureRepo() as fixture:
            fixture.write_many(
                ["lib/tests/t%d.test.mjs" % i for i in range(12)], node_test
            )
            fixture.write("tools/test_helper.py", "import unittest\n")
            rule = keys(testing.detect(fixture.repo()))["test-framework"].rule
            self.assertIn("node --test", rule)
            self.assertNotIn("unittest", rule)

    def test_a_single_import_is_not_a_convention(self):
        with FixtureRepo() as fixture:
            fixture.write("tests/test_only.py", "import unittest\n")
            fixture.write_many(
                ["tests/test_%d.py" % i for i in range(3)],
                "def test_x():\n    assert True\n",
            )
            self.assertNotIn("test-framework", keys(testing.detect(fixture.repo())))

    def test_pytest_configured_in_pyproject_or_conftest(self):
        with FixtureRepo() as fixture:
            fixture.write(
                "pyproject.toml", "[tool.pytest.ini_options]\naddopts = '-q'\n"
            )
            fixture.write_many(
                ["tests/test_%d.py" % i for i in range(3)],
                "def test_x():\n    assert True\n",
            )
            self.assertIn(
                "pytest", keys(testing.detect(fixture.repo()))["test-framework"].rule
            )
        with FixtureRepo() as fixture:
            fixture.write("tests/conftest.py", "")
            fixture.write_many(
                ["tests/test_%d.py" % i for i in range(3)],
                "def test_x():\n    assert True\n",
            )
            self.assertIn(
                "pytest", keys(testing.detect(fixture.repo()))["test-framework"].rule
            )

    def test_absent_suite_is_reported_only_for_code_repos(self):
        with FixtureRepo() as fixture:
            fixture.write_many(["src/%d.ts" % i for i in range(12)], TS_SOURCE)
            found = keys(testing.detect(fixture.repo()))
            self.assertIn("tests-absent", found)

    def test_docs_only_repo_is_not_scolded_for_missing_tests(self):
        with FixtureRepo() as fixture:
            fixture.write_many(["docs/%d.md" % i for i in range(12)], "# doc")
            self.assertEqual(testing.detect(fixture.repo()), [])


class TestStyle(unittest.TestCase):
    def test_semicolon_and_quote_conventions_from_source(self):
        with FixtureRepo() as fixture:
            fixture.write_many(["src/%d.ts" % i for i in range(12)], TS_SOURCE)
            found = keys(style.detect(fixture.repo()))
            self.assertIn("end with semicolons", found["js-semicolons"].rule)
            self.assertIn("single quotes", found["js-quotes"].rule)

    def test_semicolon_free_codebase_is_detected(self):
        with FixtureRepo() as fixture:
            fixture.write_many(["src/%d.ts" % i for i in range(12)], TS_SOURCE_NO_SEMI)
            found = keys(style.detect(fixture.repo()))
            self.assertIn("omit semicolons", found["js-semicolons"].rule)

    def test_prettier_config_conflicting_with_code_is_reported(self):
        """The most useful thing this detector can find."""
        with FixtureRepo() as fixture:
            fixture.write_many(["src/%d.ts" % i for i in range(12)], TS_SOURCE_NO_SEMI)
            fixture.write_json(".prettierrc", {"semi": True})
            found = keys(style.detect(fixture.repo()))
            self.assertIn("does not match", found["prettier"].rule)

    def test_prettier_agreeing_with_code_is_stated_plainly(self):
        with FixtureRepo() as fixture:
            fixture.write_many(["src/%d.ts" % i for i in range(12)], TS_SOURCE)
            fixture.write_json(".prettierrc", {"semi": True, "singleQuote": True})
            found = keys(style.detect(fixture.repo()))
            self.assertIn("owns formatting", found["prettier"].rule)

    def test_python_conventions(self):
        with FixtureRepo() as fixture:
            fixture.write_many(["pkg/%d.py" % i for i in range(12)], PY_SOURCE)
            found = keys(style.detect(fixture.repo()))
            self.assertIn("py-type-hints", found)
            self.assertIn("py-docstrings", found)

    def test_tiny_repo_produces_no_style_rules(self):
        with FixtureRepo() as fixture:
            fixture.write_many(["src/a.ts", "src/b.ts"], TS_SOURCE)
            found = keys(style.detect(fixture.repo()))
            self.assertNotIn("js-quotes", found)

    def test_editorconfig_is_authoritative(self):
        with FixtureRepo() as fixture:
            fixture.write(".editorconfig", "[*]\nindent_style = tab\nindent_size = 4\n")
            found = keys(style.detect(fixture.repo()))
            self.assertIn("indent_style = tab", found["editorconfig"].rule)


class TestHistory(unittest.TestCase):
    def test_conventional_commits_detected_with_types_and_scopes(self):
        with FixtureRepo(git=True) as fixture:
            for i in range(14):
                fixture.commit("feat(api): add endpoint %d" % i)
            for i in range(4):
                fixture.commit("fix(ui): correct alignment %d" % i)
            found = keys(history.detect(fixture.repo()))
            self.assertIn("commit-convention", found)
            self.assertIn("Conventional Commits", found["commit-convention"].rule)
            self.assertIn("`api`", found["commit-convention"].rule)

    def test_ticket_prefixed_commits_detected(self):
        with FixtureRepo(git=True) as fixture:
            for i in range(16):
                fixture.commit("PROJ-%d: do the thing" % i)
            found = keys(history.detect(fixture.repo()))
            self.assertIn("issue key", found["commit-convention"].rule)

    def test_inconsistent_history_produces_no_convention(self):
        with FixtureRepo(git=True) as fixture:
            for i in range(16):
                fixture.commit("random change number %d here" % i)
            found = keys(history.detect(fixture.repo()))
            self.assertNotIn("commit-convention", found)

    def test_imperative_mood_detected(self):
        with FixtureRepo(git=True) as fixture:
            for i in range(16):
                fixture.commit("Add support for feature %d" % i)
            found = keys(history.detect(fixture.repo()))
            self.assertIn("imperative", found["commit-subject-style"].rule)

    def test_past_tense_detected(self):
        with FixtureRepo(git=True) as fixture:
            for i in range(16):
                fixture.commit("Added support for feature %d" % i)
            found = keys(history.detect(fixture.repo()))
            self.assertIn("past tense", found["commit-subject-style"].rule)

    def test_non_git_directory_produces_nothing(self):
        with FixtureRepo() as fixture:
            fixture.write("README.md", "# hi")
            self.assertEqual(history.detect(fixture.repo()), [])

    def test_shallow_history_produces_nothing(self):
        with FixtureRepo(git=True) as fixture:
            fixture.commit("first")
            self.assertEqual(history.detect(fixture.repo()), [])

    def test_hot_paths_surface_frequently_changed_files(self):
        with FixtureRepo(git=True) as fixture:
            for i in range(25):
                fixture.commit("change %d" % i, touch="src/hot.ts")
            for i in range(25):
                fixture.commit("other %d" % i, touch="src/other-%d.ts" % i)
            found = keys(history.detect(fixture.repo()))
            if "hot-paths" in found:
                self.assertIn("src/hot.ts", found["hot-paths"].rule)


class TestLayout(unittest.TestCase):
    def test_naming_convention_per_directory(self):
        with FixtureRepo() as fixture:
            fixture.write_many(
                [
                    "src/components/ButtonPrimary.tsx",
                    "src/components/CardHeader.tsx",
                    "src/components/ModalDialog.tsx",
                    "src/components/NavBar.tsx",
                    "src/components/SidePanel.tsx",
                    "src/components/UserAvatar.tsx",
                    "src/components/FormField.tsx",
                ],
                TS_SOURCE,
            )
            found = keys(layout.detect(fixture.repo()))
            naming = [f for k, f in found.items() if k.startswith("naming-")]
            self.assertTrue(naming)
            self.assertIn("PascalCase", naming[0].rule)

    def test_boundaries_include_migrations_and_lockfiles(self):
        with FixtureRepo() as fixture:
            fixture.write("supabase/migrations/001_init.sql", "select 1;")
            fixture.write("package-lock.json", "{}")
            found = keys(layout.detect(fixture.repo()))
            self.assertIn("boundaries", found)
            self.assertIn("migrations", found["boundaries"].rule)
            self.assertIn("package-lock.json", found["boundaries"].rule)

    def test_alias_imports_detected_from_tsconfig(self):
        with FixtureRepo() as fixture:
            fixture.write_json(
                "tsconfig.json",
                {"compilerOptions": {"paths": {"@/*": ["./src/*"]}}},
            )
            fixture.write_many(["src/%d.ts" % i for i in range(10)], TS_SOURCE)
            found = keys(layout.detect(fixture.repo()))
            self.assertIn("import-style", found)
            self.assertIn("@/*", found["import-style"].rule)

    def test_mixed_naming_produces_no_rule(self):
        with FixtureRepo() as fixture:
            fixture.write_many(
                [
                    "src/x/AlphaBeta.ts",
                    "src/x/gamma-delta.ts",
                    "src/x/epsilon_zeta.ts",
                    "src/x/EtaTheta.ts",
                    "src/x/iota-kappa.ts",
                    "src/x/lambda_mu.ts",
                ],
                TS_SOURCE,
            )
            found = keys(layout.detect(fixture.repo()))
            self.assertFalse([k for k in found if k.startswith("naming-")])


class TestVerification(unittest.TestCase):
    def test_ci_commands_extracted_and_noise_dropped(self):
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/ci.yml",
                "on: [push, pull_request]\n"
                "jobs:\n"
                "  test:\n"
                "    steps:\n"
                "      - uses: actions/checkout@v4\n"
                "      - run: actions/checkout@v4 cache\n"
                "      - run: pnpm install\n"
                "      - run: pnpm run test\n"
                "      - run: pnpm run typecheck\n",
            )
            found = keys(verification.detect(fixture.repo()))
            rule = found["ci-commands"].rule
            self.assertIn("pnpm run typecheck", rule)
            self.assertNotIn("checkout", rule)

    def test_python_matrix_is_ordered_numerically(self):
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/ci.yml",
                "on: pull_request\njobs:\n  t:\n    strategy:\n"
                '      matrix:\n        python-version: ["3.9", "3.10", "3.13"]\n'
                "    steps:\n      - run: python -m pytest\n",
            )
            found = keys(verification.detect(fixture.repo()))
            rule = found["ci-commands"].rule
            self.assertIn("3.9, 3.10, 3.13", rule)

    def test_hooks_detected(self):
        with FixtureRepo() as fixture:
            fixture.write(".husky/pre-commit", "npx lint-staged\n")
            found = keys(verification.detect(fixture.repo()))
            self.assertIn("hooks", found)

    def test_no_ci_produces_nothing(self):
        with FixtureRepo() as fixture:
            fixture.write("README.md", "# hi")
            self.assertEqual(verification.detect(fixture.repo()), [])


def workflow(on, *steps, matrix=None, job_extra=""):
    """A minimal workflow file: one job, the given `run:` lines as steps."""
    lines = ["on: %s" % on, "jobs:", "  check:", "    runs-on: ubuntu-latest"]
    if job_extra:
        lines.append(job_extra)
    if matrix:
        lines += ["    strategy:", "      matrix:"]
        lines += ["        %s: [%s]" % (key, ", ".join(vals)) for key, vals in matrix]
    lines += ["    steps:", "      - uses: actions/checkout@v4"]
    for step in steps:
        if "\n" in step:
            lines.append("      - run: |")
            lines += ["          " + line for line in step.splitlines()]
        else:
            lines.append("      - run: %s" % step)
    return "\n".join(lines) + "\n"


class TestVerificationGates(unittest.TestCase):
    """Only CI that runs on a change may be described as what a change must pass."""

    def rule(self, fixture):
        return keys(verification.detect(fixture.repo())).get("ci-commands")

    def test_scheduled_workflow_is_not_a_gate(self):
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/nightly.yml",
                workflow("\n  schedule:\n    - cron: '0 3 * * *'", "npm run refresh"),
            )
            found = keys(verification.detect(fixture.repo()))
            self.assertNotIn("ci-commands", found)
            self.assertIn("ci-not-gating", found)
            self.assertIn("No GitHub Actions workflow", found["ci-not-gating"].rule)

    def test_manual_release_workflow_is_not_a_gate(self):
        """A TestFlight upload button is automation, not a check on changes."""
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/release.yml",
                workflow("workflow_dispatch", "npm run verify"),
            )
            self.assertIsNone(self.rule(fixture))

    def test_tag_only_push_is_not_a_gate(self):
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/release.yml",
                workflow("\n  push:\n    tags: ['v*']", "python -m build"),
            )
            self.assertIsNone(self.rule(fixture))

    def test_gate_commands_ignore_other_workflows(self):
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/ci.yml",
                workflow("[push, pull_request]", "npm test"),
            )
            fixture.write(
                ".github/workflows/nightly.yml",
                workflow("\n  schedule:\n    - cron: '0 3 * * *'", "npm run scrape"),
            )
            rule = self.rule(fixture).rule
            self.assertIn("npm test", rule)
            self.assertNotIn("scrape", rule)

    def test_multi_line_run_blocks_are_read(self):
        """A lint step written as `run: |` used to be invisible."""
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/ci.yml",
                workflow("pull_request", "npm run lint\nnpm test"),
            )
            rule = self.rule(fixture).rule
            self.assertIn("`npm run lint`", rule)
            self.assertIn("`npm test`", rule)

    def test_shell_programs_are_pointed_at_not_reproduced(self):
        """Splitting a script with a background server and a loop into
        "commands" would list `next start &` as a check."""
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/ci.yml",
                workflow(
                    "pull_request",
                    "npm run build",
                    "npx next start &\nfor i in 1 2 3; do curl -sf localhost:3000 && break; done\nnpx playwright test",
                ),
            )
            rule = self.rule(fixture).rule
            self.assertIn("npm run build", rule)
            self.assertNotIn("next start", rule)
            self.assertNotIn("playwright test", rule)
            self.assertIn("read `.github/workflows/ci.yml`", rule)


class TestVerificationExpressions(unittest.TestCase):
    def rule(self, fixture):
        return keys(verification.detect(fixture.repo()))["ci-commands"].rule

    def test_literal_matrix_values_are_expanded(self):
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/ci.yml",
                workflow(
                    "pull_request",
                    "npx playwright test --project=${{ matrix.browser }}",
                    matrix=[("browser", ["chromium", "firefox", "webkit"])],
                ),
            )
            rule = self.rule(fixture)
            self.assertIn("`npx playwright test --project=<browser>`", rule)
            self.assertIn("for each of chromium, firefox, webkit", rule)
            self.assertNotIn("${{", rule)

    def test_single_matrix_value_is_substituted(self):
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/ci.yml",
                workflow(
                    "pull_request",
                    "npm test -- --shard=${{ matrix.shard }}",
                    matrix=[("shard", ["1"])],
                ),
            )
            self.assertIn("`npm test -- --shard=1`", self.rule(fixture))

    def test_non_matrix_expressions_are_omitted(self):
        """`${{ inputs.x }}` has no value a reader could type."""
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/ci.yml",
                workflow(
                    "pull_request",
                    "npm test",
                    "npm run e2e -- --env=${{ inputs.environment }}",
                ),
            )
            rule = self.rule(fixture)
            self.assertNotIn("${{", rule)
            self.assertNotIn("e2e", rule.split("read")[0])

    def test_expression_built_matrix_is_not_expanded(self):
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/ci.yml",
                "on: pull_request\n"
                "jobs:\n  t:\n    strategy:\n      matrix:\n"
                "        browser: ${{ fromJSON(inputs.browsers) }}\n"
                "    steps:\n"
                "      - run: npm run build\n"
                "      - run: npx playwright test --project=${{ matrix.browser }}\n",
            )
            rule = self.rule(fixture)
            self.assertNotIn("${{", rule)
            self.assertNotIn("playwright test", rule.split("read")[0])

    def test_setup_steps_are_not_listed_as_checks(self):
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/ci.yml",
                workflow(
                    "pull_request",
                    "npm ci",
                    "npx playwright install --with-deps chromium",
                    "sudo apt-get install -y ffmpeg",
                    "python -m pip install -e '.[dev]'",
                    "npm test",
                ),
            )
            rule = self.rule(fixture)
            checks, _, setup = rule.partition("Before the checks")
            self.assertIn("npm test", checks)
            for command in ("npm ci", "playwright install", "apt-get", "pip install"):
                self.assertNotIn(command, checks)
                self.assertIn(command, setup)

    def test_version_probes_are_not_checks(self):
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/ci.yml",
                workflow("pull_request", "node --version", "python -V", "npm test"),
            )
            rule = self.rule(fixture)
            self.assertIn("npm test", rule)
            self.assertNotIn("--version", rule)
            self.assertNotIn("python -V", rule)

    def test_checks_whose_failures_are_ignored_say_so(self):
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/ci.yml",
                workflow("pull_request", "npm test", "npm run e2e || true"),
            )
            rule = self.rule(fixture)
            self.assertIn("`npm run e2e || true` (CI ignores its failures)", rule)
            self.assertNotIn("`npm test` (CI ignores", rule)

    def test_setup_only_workflow_produces_no_check_list(self):
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/ci.yml", workflow("pull_request", "npm ci")
            )
            self.assertNotIn("ci-commands", keys(verification.detect(fixture.repo())))

    def test_matrix_versions_come_from_gating_jobs_only(self):
        with FixtureRepo() as fixture:
            fixture.write(
                ".github/workflows/ci.yml",
                workflow(
                    "push", "pytest", matrix=[("python-version", ['"3.10"', '"3.12"'])]
                ),
            )
            fixture.write(
                ".github/workflows/legacy.yml",
                workflow(
                    "workflow_dispatch",
                    "pytest",
                    matrix=[("python-version", ['"2.7"'])],
                ),
            )
            rule = self.rule(fixture)
            self.assertIn("3.10, 3.12", rule)
            self.assertNotIn("2.7", rule)


class TestLinterClaims(unittest.TestCase):
    """CI enforcement is claimed only when a gating step is seen running the tool."""

    def eslint_repo(self, fixture, scripts):
        fixture.write_json("package.json", {"name": "x", "scripts": scripts})
        fixture.write("package-lock.json", "{}")
        fixture.write("eslint.config.mjs", "export default [];")

    def test_configured_but_not_in_ci_claims_no_enforcement(self):
        """The invented claim from a real audit: ESLint configured, CI runs
        install, build, audit — and the file said CI rejects unformatted work."""
        with FixtureRepo() as fixture:
            self.eslint_repo(fixture, {"lint": "eslint .", "build": "next build"})
            fixture.write(
                ".github/workflows/ci.yml",
                workflow(
                    "pull_request",
                    "npm ci",
                    "npm run build",
                    "npm audit --audit-level=high",
                ),
            )
            rule = keys(style.detect(fixture.repo()))["tool-eslint"].rule
            self.assertIn("ESLint (linter) is configured in `eslint.config.mjs`", rule)
            self.assertIn("Run `npm run lint` before finishing", rule)
            self.assertNotIn("CI", rule)
            self.assertNotIn("format", rule)

    def test_enforcement_through_a_script_chain_is_claimed(self):
        with FixtureRepo() as fixture:
            self.eslint_repo(
                fixture,
                {
                    "verify": "npm run typecheck && npm run lint",
                    "lint": "eslint .",
                    "typecheck": "tsc",
                },
            )
            fixture.write(
                ".github/workflows/ci.yml", workflow("pull_request", "npm run verify")
            )
            rule = keys(style.detect(fixture.repo()))["tool-eslint"].rule
            self.assertIn(
                "CI runs `npm run verify` and rejects work that fails it", rule
            )
            self.assertIn("`npm run lint`", rule)

    def test_npm_pre_hooks_count_as_the_chain(self):
        with FixtureRepo() as fixture:
            self.eslint_repo(fixture, {"pretest": "eslint .", "test": "vitest run"})
            fixture.write(
                ".github/workflows/ci.yml", workflow("pull_request", "npm test")
            )
            rule = keys(style.detect(fixture.repo()))["tool-eslint"].rule
            self.assertIn("CI runs `npm test`", rule)

    def test_lint_in_a_scheduled_workflow_is_not_enforcement(self):
        with FixtureRepo() as fixture:
            self.eslint_repo(fixture, {"lint": "eslint ."})
            fixture.write(
                ".github/workflows/weekly.yml",
                workflow("\n  schedule:\n    - cron: '0 3 * * 1'", "npm run lint"),
            )
            rule = keys(style.detect(fixture.repo()))["tool-eslint"].rule
            self.assertNotIn("CI", rule)

    def test_continue_on_error_runs_but_does_not_reject(self):
        with FixtureRepo() as fixture:
            self.eslint_repo(fixture, {"lint": "eslint ."})
            fixture.write(
                ".github/workflows/ci.yml",
                workflow(
                    "pull_request",
                    "npm run lint",
                    job_extra="    continue-on-error: true",
                ),
            )
            rule = keys(style.detect(fixture.repo()))["tool-eslint"].rule
            self.assertIn("CI runs `npm run lint`.", rule)
            self.assertNotIn("rejects", rule)

    def test_formatter_without_check_mode_is_not_enforcement(self):
        """`black .` in CI rewrites files and exits 0."""
        with FixtureRepo() as fixture:
            fixture.write("pyproject.toml", "[tool.black]\nline-length = 100\n")
            fixture.write(".github/workflows/ci.yml", workflow("push", "black ."))
            rule = keys(style.detect(fixture.repo()))["tool-black"].rule
            self.assertIn("Black (formatter)", rule)
            self.assertNotIn("rejects", rule)

    def test_formatter_in_check_mode_is_enforcement(self):
        with FixtureRepo() as fixture:
            fixture.write("pyproject.toml", "[tool.black]\nline-length = 100\n")
            fixture.write(
                ".github/workflows/ci.yml", workflow("push", "black --check .")
            )
            rule = keys(style.detect(fixture.repo()))["tool-black"].rule
            self.assertIn("rejects work that fails it", rule)

    def test_ruff_is_a_formatter_only_when_something_formats_with_it(self):
        with FixtureRepo() as fixture:
            fixture.write("pyproject.toml", "[tool.ruff]\nline-length = 100\n")
            fixture.write(".github/workflows/ci.yml", workflow("push", "ruff check ."))
            rule = keys(style.detect(fixture.repo()))["tool-ruff"].rule
            self.assertIn("Ruff (linter)", rule)
            self.assertIn("CI runs `ruff check .` and rejects", rule)

        with FixtureRepo() as fixture:
            fixture.write("pyproject.toml", "[tool.ruff]\nline-length = 100\n")
            fixture.write(
                ".github/workflows/ci.yml",
                workflow("push", "ruff check .", "ruff format --check ."),
            )
            rule = keys(style.detect(fixture.repo()))["tool-ruff"].rule
            self.assertIn("Ruff (linter and formatter)", rule)
            self.assertIn("ruff format --check .", rule)

    def test_tox_ini_alone_is_not_flake8(self):
        """tox.ini exists in repositories that have never run Flake8."""
        with FixtureRepo() as fixture:
            fixture.write("tox.ini", "[tox]\nenvlist = py39\n")
            self.assertNotIn("tool-flake8", keys(style.detect(fixture.repo())))
            fixture.write(
                "tox.ini", "[tox]\nenvlist = py39\n\n[flake8]\nmax-line-length = 100\n"
            )
            fresh = fixture.repo()
            self.assertIn("tox.ini", keys(style.detect(fresh))["tool-flake8"].rule)

    def test_python_style_rules_name_the_language(self):
        with FixtureRepo() as fixture:
            fixture.write_many(["pkg/%d.py" % i for i in range(12)], PY_SOURCE)
            found = keys(style.detect(fixture.repo()))
            self.assertTrue(found["py-docstrings"].rule.startswith("Python "))
            self.assertTrue(found["py-type-hints"].rule.startswith("Python "))


class TestFileListing(unittest.TestCase):
    """What counts as the project decides every count in the output."""

    def test_gitignored_files_are_not_sampled(self):
        with FixtureRepo(git=True) as fixture:
            fixture.write(".gitignore", "artifacts/\n")
            fixture.write("src/app.ts", TS_SOURCE)
            fixture.write_many(["artifacts/report-%d.js" % i for i in range(30)], "x")
            files = fixture.repo().files()
            self.assertIn("src/app.ts", files)
            self.assertFalse([f for f in files if f.startswith("artifacts/")])

    def test_nested_worktree_is_not_part_of_the_project(self):
        """A Claude Code worktree under `.claude/worktrees/` is a second copy
        of the repository; its migrations are not this checkout's."""
        with FixtureRepo() as fixture:
            fixture.write("src/app.ts", TS_SOURCE)
            fixture.write(".claude/worktrees/feature/.git", "gitdir: /elsewhere\n")
            fixture.write(".claude/worktrees/feature/supabase/migrations/001.sql", "x")
            repo = fixture.repo()
            self.assertFalse([f for f in repo.files() if "worktrees" in f])
            boundaries = keys(layout.detect(repo)).get("boundaries")
            self.assertTrue(boundaries is None or "worktrees" not in boundaries.rule)

    def test_worktree_checkout_is_recognised_as_git(self):
        """In a linked worktree `.git` is a file — and worktrees are where
        coding agents run. History must still be read there."""
        with FixtureRepo(git=True) as fixture:
            fixture.commit("Add the first file", touch="src/app.ts")
            fixture._git(
                "worktree", "add", "-q", ".claude/worktrees/feat", "-b", "feat"
            )
            outer = fixture.repo()
            self.assertFalse([f for f in outer.all_files() if "worktrees" in f])
            inner = Repo(os.path.join(fixture.root, ".claude", "worktrees", "feat"))
            self.assertTrue(inner.is_git)
            self.assertEqual(inner.commit_subjects(), ["Add the first file"])

    def test_tooling_files_are_findable_but_not_sampled(self):
        with FixtureRepo(git=True) as fixture:
            fixture.write(".cursor/install.sh", "echo hi")
            fixture.write("src/app.ts", TS_SOURCE)
            repo = fixture.repo()
            self.assertIn(".cursor/install.sh", repo.all_files())
            self.assertNotIn(".cursor/install.sh", repo.files())


class TestHotPathAccuracy(unittest.TestCase):
    def test_window_is_the_real_commit_count(self):
        """ "of the last 300 commits" about a 26-commit history is a small lie."""
        with FixtureRepo(git=True) as fixture:
            # The detector needs 20 distinct paths and one changed 4+ times.
            for i in range(6):
                fixture.commit("change %d" % i, touch="src/hot.ts")
            for i in range(20):
                fixture.commit("other %d" % i, touch="src/other-%d.ts" % i)
            rule = keys(history.detect(fixture.repo()))["hot-paths"].rule
            self.assertIn("of the last 26 commits", rule)
            self.assertNotIn("300", rule)

    def test_deleted_files_are_not_listed(self):
        with FixtureRepo(git=True) as fixture:
            for i in range(8):
                fixture.commit("change %d" % i, touch="src/gone.ts")
            for i in range(20):
                fixture.commit("other %d" % i, touch="src/other-%d.ts" % i)
            for i in range(5):
                fixture.commit("keep %d" % i, touch="src/kept.ts")
            fixture._git("rm", "-q", "src/gone.ts")
            fixture._git("commit", "-q", "-m", "Remove gone")
            rule = keys(history.detect(fixture.repo()))["hot-paths"].rule
            self.assertNotIn("src/gone.ts", rule)
            self.assertIn("src/kept.ts", rule)


class TestDetectorIsolation(unittest.TestCase):
    def test_a_failing_detector_does_not_kill_the_run(self):
        """Real repositories contain malformed files. One should not abort all."""
        import agentsmith.detectors as registry

        def exploding(_repo):
            raise ValueError("boom")

        original = registry.DETECTORS
        registry.DETECTORS = (("boom", exploding), *original)
        try:
            with FixtureRepo() as fixture:
                fixture.write_json("package.json", {"name": "x"})
                fixture.write("pnpm-lock.yaml", "")
                findings = run_all(fixture.repo())
                found = keys(findings)
                self.assertIn("detector-error-boom", found)
                self.assertIn("package-manager", found)
        finally:
            registry.DETECTORS = original


if __name__ == "__main__":
    unittest.main()
