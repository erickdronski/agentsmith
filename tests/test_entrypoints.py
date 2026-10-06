"""Smoke tests that execute the package the way a user does.

The unit suites import `main()` directly, which never runs the
`if __name__ == "__main__":` block. A real bug once lived exactly there — an
automated fix appended `from exc` to `raise SystemExit(main())`, referencing
a name that does not exist at module scope. Every test passed; running the
command would have raised NameError.

So these run the entry points as subprocesses. They are slow relative to the
rest of the suite and they cover the one path nothing else does.
"""

import os
import re
import subprocess
import sys
import unittest

import agentsmith

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestEntryPoints(unittest.TestCase):
    def run_module(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "agentsmith", *args],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_module_executes_as_main(self):
        result = self.run_module("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("agentsmith", result.stdout + result.stderr)

    def test_version_matches_the_package_metadata(self):
        """`--version` and pyproject.toml are two copies of one fact, and the
        kind of thing a release bumps in one place and forgets in the other."""
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as handle:
            declared = re.search(r'^version = "([^"]+)"', handle.read(), re.M).group(1)
        self.assertEqual(agentsmith.__version__, declared)
        result = self.run_module("--version")
        self.assertIn(declared, result.stdout + result.stderr)

    def test_help_executes_as_main(self):
        result = self.run_module("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage", (result.stdout + result.stderr).lower())

    def test_help_survives_a_windows_console_encoding(self):
        """Windows pipes `--help` through cp1252; one arrow in a help string
        raises UnicodeEncodeError there and nowhere else."""
        from agentsmith.cli import build_parser

        build_parser().format_help().encode("cp1252")

    def test_no_warnings_on_import(self):
        """A SyntaxWarning from an invalid escape would surface here."""
        result = subprocess.run(
            [sys.executable, "-W", "error::SyntaxWarning", "-c", "import agentsmith"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
