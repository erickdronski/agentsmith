"""Tests for the YAML subset parser.

Every case here is a shape that appears in real GitHub Actions workflows. The
parser only has to be right about those, but it has to be right about them
exactly, because a misread trigger or matrix becomes a false sentence in an
AGENTS.md.
"""

import unittest

from agentsmith.miniyaml import parse

WORKFLOW = """\
name: CI  # trailing comment

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest]
        python-version: ["3.9", "3.10", 3.11]
        include:
          - { os: macos-latest, python-version: "3.12" }
    steps:
      - uses: actions/checkout@v4
      - name: Install
        run: pip install -e '.[dev]'
      - name: Test
        run: |
          pytest -q
          # a comment inside a script is part of the script
          ruff check .
      - run: >
          python -m build
          --wheel
"""


class TestStructure(unittest.TestCase):
    def setUp(self):
        self.doc = parse(WORKFLOW)

    def test_on_is_a_string_key_not_a_boolean(self):
        """YAML 1.1 reads a bare `on` as `true`. Workflows mean the event key."""
        self.assertIn("on", self.doc)
        self.assertEqual(self.doc["on"]["push"], {"branches": ["main"]})
        self.assertIn("pull_request", self.doc["on"])

    def test_comments_are_stripped(self):
        self.assertEqual(self.doc["name"], "CI")

    def test_versions_stay_strings(self):
        """`3.10` must not become the float 3.1."""
        matrix = self.doc["jobs"]["test"]["strategy"]["matrix"]
        self.assertEqual(matrix["python-version"], ["3.9", "3.10", "3.11"])

    def test_flow_mapping_inside_a_sequence(self):
        include = self.doc["jobs"]["test"]["strategy"]["matrix"]["include"]
        self.assertEqual(include, [{"os": "macos-latest", "python-version": "3.12"}])

    def test_sequence_of_mappings(self):
        steps = self.doc["jobs"]["test"]["steps"]
        self.assertEqual(steps[0], {"uses": "actions/checkout@v4"})
        self.assertEqual(steps[1]["run"], "pip install -e '.[dev]'")

    def test_literal_block_keeps_lines_and_comments(self):
        run = self.doc["jobs"]["test"]["steps"][2]["run"]
        self.assertEqual(
            run.splitlines(),
            [
                "pytest -q",
                "# a comment inside a script is part of the script",
                "ruff check .",
            ],
        )

    def test_folded_block_joins_lines(self):
        self.assertEqual(
            self.doc["jobs"]["test"]["steps"][3]["run"], "python -m build --wheel"
        )

    def test_expressions_survive_as_text(self):
        self.assertEqual(self.doc["jobs"]["test"]["runs-on"], "${{ matrix.os }}")


class TestEdgeCases(unittest.TestCase):
    def test_trigger_list_and_scalar_forms(self):
        self.assertEqual(
            parse("on: [push, pull_request]\n")["on"], ["push", "pull_request"]
        )
        self.assertEqual(parse("on: push\n")["on"], "push")

    def test_hash_inside_quotes_and_urls_is_not_a_comment(self):
        doc = parse('a: "x # y"\nb: https://example.com/#frag\nc: it\'s # gone\n')
        self.assertEqual(doc["a"], "x # y")
        self.assertEqual(doc["b"], "https://example.com/#frag")
        self.assertEqual(doc["c"], "it's")

    def test_sequence_at_the_same_indent_as_its_key(self):
        doc = parse("branches:\n- main\n- dev\nnext: 1\n")
        self.assertEqual(doc["branches"], ["main", "dev"])
        self.assertEqual(doc["next"], "1")

    def test_multi_line_flow_sequence(self):
        doc = parse('versions: [\n  "3.9",\n  "3.10",\n]\nafter: ok\n')
        self.assertEqual(doc["versions"], ["3.9", "3.10"])
        self.assertEqual(doc["after"], "ok")

    def test_expression_valued_matrix_is_left_as_a_string(self):
        doc = parse("matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}\n")
        self.assertEqual(doc["matrix"], "${{ fromJSON(needs.plan.outputs.matrix) }}")

    def test_empty_document(self):
        self.assertIsNone(parse("# only a comment\n"))


if __name__ == "__main__":
    unittest.main()
