"""Code style, read from the code rather than only from the config.

Config files state intent. Source files state practice. When they disagree —
a Prettier config nobody runs, an ESLint rule disabled inline across half the
codebase — practice is what an agent should match, and the disagreement itself
is worth reporting.

So this detector reads both, and says so when they diverge.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from ..evidence import Confidence, Evidence, Finding, dominant
from ..repo import Repo

SECTION = "Code style"

JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
PY_EXTENSIONS = (".py",)

PRETTIER_FILES = (
    ".prettierrc",
    ".prettierrc.json",
    ".prettierrc.yml",
    ".prettierrc.yaml",
    ".prettierrc.js",
    "prettier.config.js",
    ".prettierrc.cjs",
)

LINT_CONFIGS = (
    (
        "ESLint",
        (
            ".eslintrc",
            ".eslintrc.json",
            ".eslintrc.js",
            ".eslintrc.cjs",
            ".eslintrc.yml",
            "eslint.config.js",
            "eslint.config.mjs",
        ),
    ),
    ("Biome", ("biome.json", "biome.jsonc")),
    ("Ruff", ("ruff.toml", ".ruff.toml")),
    ("Black", ()),
    ("Flake8", (".flake8", "tox.ini")),
    ("golangci-lint", (".golangci.yml", ".golangci.yaml")),
    ("RuboCop", (".rubocop.yml",)),
    ("SwiftLint", (".swiftlint.yml",)),
    ("Clippy", ("clippy.toml",)),
)


def detect(repo: Repo) -> List[Finding]:
    findings: List[Finding] = []

    findings.extend(_linters(repo))

    js_files = [
        p
        for p in repo.files_matching(JS_EXTENSIONS)
        if "/test" not in p and ".test." not in p and ".spec." not in p
    ]
    if len(js_files) >= 8:
        findings.extend(_javascript_style(repo, js_files))

    py_files = repo.files_matching(PY_EXTENSIONS)
    if len(py_files) >= 8:
        findings.extend(_python_style(repo, py_files))

    findings.extend(_editorconfig(repo))
    return findings


def _linters(repo: Repo) -> List[Finding]:
    present: List[Tuple[str, str]] = []
    for name, files in LINT_CONFIGS:
        for filename in files:
            if repo.exists(filename):
                present.append((name, filename))
                break

    pyproject = repo.read("pyproject.toml") or ""
    if "[tool.ruff" in pyproject and not any(n == "Ruff" for n, _ in present):
        present.append(("Ruff", "pyproject.toml"))
    if "[tool.black" in pyproject:
        present.append(("Black", "pyproject.toml"))
    if "[tool.mypy" in pyproject:
        present.append(("mypy", "pyproject.toml"))

    if not present:
        return []

    return [
        Finding(
            key="linters",
            section=SECTION,
            rule=(
                "Linting and formatting are configured: %s. Run them before "
                "finishing — CI will reject work that has not been formatted."
                % ", ".join("%s (`%s`)" % (n, f) for n, f in present)
            ),
            confidence=Confidence.CERTAIN,
            evidence=[Evidence(f, "%s configuration" % n) for n, f in present],
        )
    ]


def _javascript_style(repo: Repo, files: List[str]) -> List[Finding]:
    findings: List[Finding] = []
    samples = repo.sample_text(files, limit=200)
    if len(samples) < 8:
        return findings

    semis: Dict[str, int] = {}
    quotes: Dict[str, int] = {}
    indents: Dict[str, int] = {}

    for _path, text in samples:
        lines = text.splitlines()[:400]

        statement_lines = [
            line.rstrip()
            for line in lines
            if re.match(r"^\s*(const|let|var|return|import|export const)\b", line)
            and not line.rstrip().endswith(("{", "(", ",", "=>"))
        ]
        if len(statement_lines) >= 3:
            with_semi = sum(1 for line in statement_lines if line.endswith(";"))
            key = (
                "end with semicolons"
                if with_semi > len(statement_lines) / 2
                else "omit semicolons"
            )
            semis[key] = semis.get(key, 0) + 1

        single = len(re.findall(r"'[^'\n]{0,80}'", text))
        double = len(re.findall(r'"[^"\n]{0,80}"', text))
        if single + double >= 4:
            key = "single quotes" if single > double else "double quotes"
            quotes[key] = quotes.get(key, 0) + 1

        indent = _detect_indent(lines)
        if indent:
            indents[indent] = indents.get(indent, 0) + 1

    for counts, key, template in (
        (semis, "semicolons", "Statements %s."),
        (quotes, "quotes", "String literals use %s."),
        (indents, "indent", "Indentation is %s."),
    ):
        result = dominant(counts)
        if not result:
            continue
        findings.append(
            Finding(
                key="js-%s" % key,
                section=SECTION,
                rule=template % result["value"],
                confidence=result["confidence"],
                evidence=[
                    Evidence(
                        "source files (%s)" % ", ".join(JS_EXTENSIONS[:3]),
                        "%s in the majority of sampled files" % result["value"],
                        observed=result["observed"],
                        total=result["total"],
                        samples=[p for p, _ in samples[:4]],
                    )
                ],
            )
        )

    prettier = next((f for f in PRETTIER_FILES if repo.exists(f)), None)
    package = repo.read_json("package.json") or {}
    if not prettier and "prettier" in package:
        prettier = "package.json"

    if prettier:
        config = repo.read_json(prettier) if prettier.endswith(("json", "rc")) else None
        if prettier == "package.json":
            config = (
                package.get("prettier")
                if isinstance(package.get("prettier"), dict)
                else None
            )
        declared = _prettier_expectations(config or {})
        conflict = _style_conflict(declared, quotes, semis)
        findings.append(
            Finding(
                key="prettier",
                section=SECTION,
                rule=(
                    "Prettier owns formatting (`%s`). Do not hand-format; run "
                    "the formatter." % prettier
                    if not conflict
                    else "Prettier is configured (`%s`), but the committed code "
                    "does not match it: %s. Match the committed code and raise "
                    "the discrepancy — it usually means the formatter is not "
                    "wired into CI." % (prettier, conflict)
                ),
                confidence=Confidence.CERTAIN,
                evidence=[Evidence(prettier, "Prettier configuration")],
            )
        )

    return findings


def _prettier_expectations(config: dict) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if "singleQuote" in config:
        out["quotes"] = "single quotes" if config["singleQuote"] else "double quotes"
    if "semi" in config:
        out["semicolons"] = (
            "end with semicolons" if config["semi"] else "omit semicolons"
        )
    return out


def _style_conflict(
    declared: Dict[str, str], quotes: Dict[str, int], semis: Dict[str, int]
) -> Optional[str]:
    problems = []
    for key, counts in (("quotes", quotes), ("semicolons", semis)):
        expected = declared.get(key)
        if not expected:
            continue
        result = dominant(counts)
        if result and result["value"] != expected and result["share"] > 0.75:
            problems.append(
                "config says %s, code uses %s in %.0f%% of files"
                % (expected, result["value"], result["share"] * 100)
            )
    return "; ".join(problems) if problems else None


def _python_style(repo: Repo, files: List[str]) -> List[Finding]:
    findings: List[Finding] = []
    samples = repo.sample_text(files, limit=200)
    if len(samples) < 8:
        return findings

    typed = 0
    docstrings = 0
    for _, text in samples:
        if re.search(r"^\s*def [a-zA-Z_]+\([^)]*\)\s*->", text, re.M):
            typed += 1
        if re.search(r'^\s*("""|\'\'\')', text, re.M):
            docstrings += 1

    total = len(samples)
    if typed / total >= 0.6:
        findings.append(
            Finding(
                key="py-type-hints",
                section=SECTION,
                rule="Functions carry return type annotations. Match this.",
                confidence=Confidence.STRONG
                if typed / total >= 0.85
                else Confidence.LIKELY,
                evidence=[
                    Evidence(
                        "Python source files",
                        "Annotated return types present",
                        observed=typed,
                        total=total,
                    )
                ],
            )
        )

    if docstrings / total >= 0.6:
        findings.append(
            Finding(
                key="py-docstrings",
                section=SECTION,
                rule="Modules and functions carry docstrings. Match this.",
                confidence=Confidence.STRONG
                if docstrings / total >= 0.85
                else Confidence.LIKELY,
                evidence=[
                    Evidence(
                        "Python source files",
                        "Docstrings present",
                        observed=docstrings,
                        total=total,
                    )
                ],
            )
        )

    line_length = _line_length(repo)
    if line_length:
        findings.append(line_length)

    return findings


def _line_length(repo: Repo) -> Optional[Finding]:
    pyproject = repo.read("pyproject.toml") or ""
    match = re.search(r"^\s*line-length\s*=\s*(\d+)", pyproject, re.M)
    if not match:
        match = re.search(r"^\s*line_length\s*=\s*(\d+)", pyproject, re.M)
    if not match:
        return None
    return Finding(
        key="py-line-length",
        section=SECTION,
        rule="Lines are limited to %s characters." % match.group(1),
        confidence=Confidence.CERTAIN,
        evidence=[Evidence("pyproject.toml", match.group(0).strip())],
    )


def _detect_indent(lines: List[str]) -> Optional[str]:
    tabs = 0
    spaces: Dict[int, int] = {}
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("\t"):
            tabs += 1
        elif line.startswith(" "):
            width = len(line) - len(line.lstrip(" "))
            if width in (2, 4, 8):
                spaces[width] = spaces.get(width, 0) + 1
    total_spaces = sum(spaces.values())
    if tabs > total_spaces and tabs >= 3:
        return "tabs"
    if not spaces:
        return None
    width = max(spaces.items(), key=lambda item: item[1])[0]
    if spaces[width] < 3:
        return None
    return "%d spaces" % width


def _editorconfig(repo: Repo) -> List[Finding]:
    text = repo.read(".editorconfig")
    if not text:
        return []
    settings = []
    for key in ("indent_style", "indent_size", "max_line_length", "end_of_line"):
        match = re.search(r"^\s*%s\s*=\s*(\S+)" % key, text, re.M)
        if match:
            settings.append("%s = %s" % (key, match.group(1)))
    if not settings:
        return []
    return [
        Finding(
            key="editorconfig",
            section=SECTION,
            rule="`.editorconfig` is authoritative for whitespace: %s."
            % ", ".join(settings),
            confidence=Confidence.CERTAIN,
            evidence=[Evidence(".editorconfig", "; ".join(settings))],
        )
    ]
