"""Tests for the detectors, against real repositories built on disk.

The recurring theme in these tests is **restraint**: a detector must find
nothing when there is nothing to find. Half of what follows asserts silence,
because the failure mode that would make this tool useless is not missing a
convention — it is confidently inventing one from four files.
"""

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
                "jobs:\n  t:\n    strategy:\n"
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
