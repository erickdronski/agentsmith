"""Where tests live, what they are called, and which framework runs them.

Agents get test conventions wrong constantly, and the failure is quiet: a test
written in the wrong place with the wrong naming still passes locally and simply
never runs in CI. Determining the convention from the existing corpus rather
than from a config file catches the cases where the config permits several
layouts and the team has settled on one.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from ..evidence import Confidence, Evidence, Finding, dominant
from ..repo import Repo

SECTION = "Tests"

FRAMEWORK_MARKERS = (
    ("vitest", ("vitest.config.ts", "vitest.config.js", "vitest.config.mts")),
    ("jest", ("jest.config.js", "jest.config.ts", "jest.config.mjs", "jest.config.json")),
    ("playwright", ("playwright.config.ts", "playwright.config.js")),
    ("cypress", ("cypress.config.ts", "cypress.config.js")),
    ("pytest", ("pytest.ini", "conftest.py")),
    ("mocha", (".mocharc.json", ".mocharc.yml")),
    ("karma", ("karma.conf.js",)),
)

TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|"
    r"[._-](test|spec)\.[a-z]+$|"
    r"(^|/)test_[^/]+\.py$|"
    r"(^|/)[^/]+_test\.(py|go|rb)$",
    re.IGNORECASE,
)

SOURCE_EXTENSIONS = (
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rb",
    ".rs", ".java", ".kt", ".swift", ".php",
)


def detect(repo: Repo) -> List[Finding]:
    findings: List[Finding] = []

    test_files = [p for p in repo.files() if TEST_FILE_RE.search(p)]

    framework = _framework(repo, test_files)
    if framework:
        findings.append(framework)

    if len(test_files) < 3:
        if _looks_like_code_repo(repo):
            findings.append(
                Finding(
                    key="tests-absent",
                    section=SECTION,
                    rule=(
                        "No meaningful test suite was found. Before adding "
                        "tests, ask which convention the maintainers want — "
                        "there is no existing pattern to follow here."
                    ),
                    confidence=Confidence.CERTAIN,
                    evidence=[
                        Evidence(
                            "file scan",
                            "%d file(s) matched a test-file pattern"
                            % len(test_files),
                        )
                    ],
                )
            )
        return findings

    location = _location(repo, test_files)
    if location:
        findings.append(location)

    naming = _naming(test_files)
    if naming:
        findings.append(naming)

    style = _assertion_style(repo, test_files)
    if style:
        findings.append(style)

    return findings


def _looks_like_code_repo(repo: Repo) -> bool:
    return len(repo.files_matching(SOURCE_EXTENSIONS, limit=15)) >= 10


def _framework(repo: Repo, test_files: List[str]) -> Optional[Finding]:
    found = []
    for name, markers in FRAMEWORK_MARKERS:
        for marker in markers:
            if repo.exists(marker):
                found.append((name, marker))
                break

    package = repo.read_json("package.json") or {}
    dev_deps = {}
    for key in ("dependencies", "devDependencies"):
        value = package.get(key)
        if isinstance(value, dict):
            dev_deps.update(value)
    for name in ("vitest", "jest", "mocha", "@playwright/test", "cypress", "ava"):
        if name in dev_deps and not any(f[0] in name for f in found):
            found.append((name.lstrip("@").split("/")[0], "package.json"))

    if not found:
        # unittest leaves no config file, so infer it from the imports.
        python_tests = [p for p in test_files if p.endswith(".py")]
        if python_tests:
            samples = repo.sample_text(python_tests, limit=40)
            uses_unittest = sum(
                1 for _, text in samples if re.search(r"^import unittest", text, re.M)
            )
            uses_pytest = sum(
                1 for _, text in samples if re.search(r"^import pytest", text, re.M)
            )
            if uses_unittest or uses_pytest:
                winner = "unittest" if uses_unittest >= uses_pytest else "pytest"
                return Finding(
                    key="test-framework",
                    section=SECTION,
                    rule="Tests run under `%s`." % winner,
                    confidence=Confidence.STRONG,
                    evidence=[
                        Evidence(
                            "test file imports",
                            "unittest in %d file(s), pytest in %d"
                            % (uses_unittest, uses_pytest),
                            observed=max(uses_unittest, uses_pytest),
                            total=len(samples),
                        )
                    ],
                )
        return None

    primary = found[0]
    extras = [name for name, _ in found[1:]]
    rule = "Tests run under `%s`." % primary[0]
    if extras:
        rule += (
            " Also present: %s — check which suite a given file belongs to "
            "before adding tests to it." % ", ".join(extras)
        )
    return Finding(
        key="test-framework",
        section=SECTION,
        rule=rule,
        confidence=Confidence.CERTAIN,
        evidence=[Evidence(marker, "Config for %s" % name) for name, marker in found],
    )


def _location(repo: Repo, test_files: List[str]) -> Optional[Finding]:
    """Co-located next to source, or gathered in a top-level directory?"""
    counts: Dict[str, int] = {}
    samples: Dict[str, List[str]] = {}

    for path in test_files:
        parts = path.split("/")
        if "__tests__" in parts:
            key = "in `__tests__` directories beside the code under test"
        elif parts[0] in ("test", "tests", "spec", "specs"):
            key = "in a top-level `%s/` directory" % parts[0]
        elif len(parts) > 1 and parts[-2] in ("test", "tests", "spec", "specs"):
            key = "in nested `%s/` directories" % parts[-2]
        else:
            key = "co-located beside the file under test"
        counts[key] = counts.get(key, 0) + 1
        samples.setdefault(key, []).append(path)

    result = dominant(counts, min_sample=4)
    if not result:
        return None

    return Finding(
        key="test-location",
        section=SECTION,
        rule="Tests live %s." % result["value"],
        confidence=result["confidence"],
        evidence=[
            Evidence(
                "test file paths",
                "Tests placed %s" % result["value"],
                observed=result["observed"],
                total=result["total"],
                samples=samples[result["value"]],
            )
        ],
        note=_runners_up_note(result),
    )


def _naming(test_files: List[str]) -> Optional[Finding]:
    counts: Dict[str, int] = {}
    samples: Dict[str, List[str]] = {}

    patterns = (
        (re.compile(r"\.test\.[a-z]+$"), "`name.test.ext`"),
        (re.compile(r"\.spec\.[a-z]+$"), "`name.spec.ext`"),
        (re.compile(r"(^|/)test_[^/]+\.py$"), "`test_name.py`"),
        (re.compile(r"_test\.(py|go|rb)$"), "`name_test.ext`"),
        (re.compile(r"(^|/)[^/]*[Ss]pec\.[a-z]+$"), "`nameSpec.ext`"),
    )

    for path in test_files:
        for pattern, label in patterns:
            if pattern.search(path):
                counts[label] = counts.get(label, 0) + 1
                samples.setdefault(label, []).append(path)
                break

    result = dominant(counts, min_sample=4)
    if not result:
        return None

    return Finding(
        key="test-naming",
        section=SECTION,
        rule="Test files are named %s." % result["value"],
        confidence=result["confidence"],
        evidence=[
            Evidence(
                "test file names",
                "Named %s" % result["value"],
                observed=result["observed"],
                total=result["total"],
                samples=samples[result["value"]],
            )
        ],
        note=_runners_up_note(result),
    )


def _assertion_style(repo: Repo, test_files: List[str]) -> Optional[Finding]:
    """describe/it versus test(), expect() versus assert."""
    samples = repo.sample_text(test_files, limit=120)
    if len(samples) < 5:
        return None

    describe = sum(1 for _, t in samples if re.search(r"\bdescribe\s*\(", t))
    bare_test = sum(
        1 for _, t in samples
        if re.search(r"^\s*(test|it)\s*\(", t, re.M)
        and not re.search(r"\bdescribe\s*\(", t)
    )

    counts = {}
    if describe:
        counts["grouped in `describe()` blocks"] = describe
    if bare_test:
        counts["written as top-level `test()` or `it()` calls"] = bare_test

    result = dominant(counts, min_sample=5)
    if not result:
        return None

    return Finding(
        key="test-structure",
        section=SECTION,
        rule="Tests are %s." % result["value"],
        confidence=result["confidence"],
        evidence=[
            Evidence(
                "test file contents",
                "Structure: %s" % result["value"],
                observed=result["observed"],
                total=result["total"],
                samples=[p for p, _ in samples[:5]],
            )
        ],
    )


def _runners_up_note(result: dict) -> Optional[str]:
    runners = [item for item in result.get("runners_up", []) if item[1] > 0]
    if not runners or result["confidence"] == Confidence.STRONG:
        return None
    return "Not universal — also seen: %s." % ", ".join(
        "%s (%d)" % (name, count) for name, count in runners
    )
