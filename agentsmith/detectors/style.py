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

from ..ci import expand, gate_commands, script_runner
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


class Tool:
    """A linter, formatter, or type checker, and how to recognise it running.

    ``role`` is what the tool does, because the sentence an agent reads has to
    match: ESLint finds problems, it does not format code, and calling it a
    formatter tells an agent the wrong thing to expect.

    ``runs`` matches a command (or an expanded script body) that invokes the
    tool. ``checks`` is the stricter test for formatters: ``black .`` in CI
    rewrites files and exits 0, so only ``black --check`` means CI rejects
    unformatted code. A linter fails on findings without any flag, so for a
    linter ``checks`` is ``None`` and running it is enough.
    """

    __slots__ = ("actions", "checks", "command", "configs", "name", "role", "runs")

    def __init__(
        self,
        name: str,
        role: str,
        configs: Tuple[str, ...],
        runs: str,
        command: Optional[str],
        checks: Optional[str] = None,
        actions: Tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.role = role
        self.configs = configs
        self.runs = re.compile(runs)
        self.command = command
        self.checks = re.compile(checks) if checks else None
        self.actions = actions


TOOLS = (
    Tool(
        "ESLint",
        "linter",
        (
            ".eslintrc",
            ".eslintrc.json",
            ".eslintrc.js",
            ".eslintrc.cjs",
            ".eslintrc.yml",
            ".eslintrc.yaml",
            "eslint.config.js",
            "eslint.config.mjs",
            "eslint.config.cjs",
            "eslint.config.ts",
        ),
        r"(?<![\w-])eslint(?![\w-])|\b(next|expo)\s+lint\b|pre-commit hook eslint\b",
        "npx eslint .",
    ),
    Tool(
        "Biome",
        "linter and formatter",
        ("biome.json", "biome.jsonc"),
        r"\bbiome\s+(check|ci|lint|format)\b",
        "npx biome check .",
        checks=r"\bbiome\s+(ci|check|lint)\b(?!.*--(write|apply))",
        actions=("biomejs/setup-biome",),
    ),
    Tool(
        "Ruff",
        "linter",
        ("ruff.toml", ".ruff.toml"),
        r"\bruff\s+(check|format)\b|\bruff\s+(?!-)[.\w/]|pre-commit hook ruff",
        "ruff check .",
        # `ruff format` without --check rewrites files and exits 0.
        checks=(
            r"\bruff\s+check\b|\bruff\s+format\b.*\s--check\b"
            r"|\bruff\s+(?!format\b|-)[.\w/]|pre-commit hook ruff"
        ),
        actions=("astral-sh/ruff-action", "chartboost/ruff-action"),
    ),
    Tool(
        "Black",
        "formatter",
        (),
        r"(?<![\w-])black(?![\w-])|pre-commit hook black\b",
        "black .",
        checks=r"(?<![\w-])black\b.*\s--check\b|pre-commit hook black\b",
        actions=("psf/black",),
    ),
    Tool(
        "Flake8",
        "linter",
        (".flake8",),
        r"\bflake8\b",
        "flake8",
    ),
    Tool(
        "mypy",
        "type checker",
        ("mypy.ini", ".mypy.ini"),
        r"\b(d?mypy)\b",
        None,
    ),
    Tool(
        "golangci-lint",
        "linter",
        (".golangci.yml", ".golangci.yaml", ".golangci.toml"),
        r"\bgolangci-lint\s+run\b",
        "golangci-lint run",
        actions=("golangci/golangci-lint-action",),
    ),
    Tool("RuboCop", "linter", (".rubocop.yml",), r"\brubocop\b", "bundle exec rubocop"),
    Tool("SwiftLint", "linter", (".swiftlint.yml",), r"\bswiftlint\b", "swiftlint"),
    Tool("Clippy", "linter", ("clippy.toml",), r"\bcargo\s+clippy\b", "cargo clippy"),
)

#: Ruff is a linter that also ships a formatter. Whether *this project* formats
#: with it is a separate fact, and only said when something shows it.
RUFF_FORMAT_RE = re.compile(r"\bruff\s+format\b|pre-commit hook ruff-format\b")

#: Commands that only report a tool's version or help, not a check.
TRIVIAL_RE = re.compile(r"\s--(version|help)\b")


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
    present = _configured_tools(repo)
    if not present:
        return []

    scan = gate_commands(repo)
    expanded = [
        (command, expand(repo, command.raw, command.working_directory))
        for command in scan.commands
        if command.kind == "check"
    ]
    scripts = _local_scripts(repo)

    findings: List[Finding] = []
    for tool, config in present:
        enforced, ran = _ci_runs(tool, expanded, scan.uses)
        role = tool.role
        local = _local_command(tool, scripts)
        extra_local: List[str] = []
        if tool.name == "Ruff" and _formats_with_ruff(repo, expanded, scripts):
            role = "linter and formatter"
            fmt = next(
                (name for name, body in scripts if RUFF_FORMAT_RE.search(body)), None
            )
            extra_local.append(fmt or "ruff format .")

        rule = "%s (%s) is configured in `%s`." % (tool.name, role, config)
        evidence = [Evidence(config, "%s configuration" % tool.name)]
        if enforced:
            rule += " CI runs %s and rejects work that fails %s." % (
                _and(enforced),
                "it" if len(enforced) == 1 else "them",
            )
            evidence.append(
                Evidence(
                    "GitHub Actions workflows",
                    "a gating step runs %s and fails the build on its findings"
                    % tool.name,
                    samples=[_strip_ticks(item) for item in enforced],
                )
            )
        elif ran:
            # Observed running, but not in a way that fails the build: a
            # formatter without --check, a `continue-on-error` step. Saying
            # what runs is a fact; claiming it rejects work would not be.
            rule += " CI runs %s." % _and(ran)
            evidence.append(
                Evidence(
                    "GitHub Actions workflows",
                    "a gating step runs %s without failing on its findings" % tool.name,
                    samples=[_strip_ticks(item) for item in ran],
                )
            )
        rule += _local_sentence([c for c in [local, *extra_local] if c], enforced + ran)

        findings.append(
            Finding(
                key="tool-%s" % tool.name.lower(),
                section=SECTION,
                rule=rule,
                confidence=Confidence.CERTAIN,
                evidence=evidence,
            )
        )
    return findings


def _configured_tools(repo: Repo) -> List[Tuple[Tool, str]]:
    present: List[Tuple[Tool, str]] = []
    pyproject = repo.read("pyproject.toml") or ""
    package = repo.read_json("package.json") or {}
    for tool in TOOLS:
        config = next((f for f in tool.configs if repo.exists(f)), None)
        if config is None:
            config = _embedded_config(repo, tool.name, pyproject, package)
        if config:
            present.append((tool, config))
    return present


def _embedded_config(
    repo: Repo, name: str, pyproject: str, package: dict
) -> Optional[str]:
    """Configuration that lives inside a shared file rather than its own.

    A shared file only counts when it has the tool's own section. `tox.ini`
    exists in plenty of repositories that have never run Flake8, and naming
    Flake8 as "configured" because tox is present invents a linter.
    """
    sections = {
        "Ruff": r"^\[tool\.ruff\b",
        "Black": r"^\[tool\.black\b",
        "mypy": r"^\[tool\.mypy\b",
    }
    if name in sections and re.search(sections[name], pyproject, re.M):
        return "pyproject.toml"
    if name == "Flake8":
        for filename in ("setup.cfg", "tox.ini"):
            if re.search(r"^\[flake8\]", repo.read(filename) or "", re.M):
                return filename
    if name == "mypy" and re.search(r"^\[mypy\]", repo.read("setup.cfg") or "", re.M):
        return "setup.cfg"
    if name == "ESLint" and isinstance(package.get("eslintConfig"), dict):
        return "package.json"
    return None


def _ci_runs(tool: Tool, expanded, uses) -> Tuple[List[str], List[str]]:
    """CI commands that run ``tool``: (enforcing, merely running).

    A command counts if it invokes the tool directly or reaches it through
    package scripts — `npm run verify` whose script runs `npm run lint` whose
    script runs `eslint .` is CI enforcing ESLint, and it is reported as
    `npm run verify` because that is the step a reader can find.
    """
    enforced: List[str] = []
    ran: List[str] = []
    for command, chain in expanded:
        hits = [
            text
            for text in chain
            if tool.runs.search(text) and not TRIVIAL_RE.search(text)
        ]
        if not hits:
            continue
        label = "`%s`" % command.display
        strict = tool.checks is None or any(tool.checks.search(text) for text in hits)
        swallowed = any(
            re.search(r"\|\|\s*(true|:|exit\s+0)\b", text) for text in chain
        )
        if command.enforcing and strict and not swallowed:
            if label not in enforced:
                enforced.append(label)
        elif label not in ran:
            ran.append(label)
    for _workflow, _step, action, _inputs, enforcing in uses:
        name = action.split("@", 1)[0]
        if name not in tool.actions:
            continue
        label = "the `%s` action" % name
        if enforcing and label not in enforced:
            enforced.append(label)
        elif not enforcing and label not in ran:
            ran.append(label)
    return enforced, ran


def _local_scripts(repo: Repo) -> List[Tuple[str, str]]:
    """(how to run it, what it runs) for each package script, fully expanded."""
    package = repo.read_json("package.json") or {}
    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        return []
    runner = script_runner(repo)
    out = []
    for name, body in scripts.items():
        expanded = " && ".join(expand(repo, str(body)))
        out.append(("%s %s" % (runner, name), expanded))
    return out


def _local_command(tool: Tool, scripts: List[Tuple[str, str]]) -> Optional[str]:
    """The project's own way to run a tool, or its standard invocation.

    A script that runs the tool and little else (`lint: eslint .`) is
    preferred over one that runs it among many (`verify: ... && npm run lint
    && ...`), because the reader wants the narrowest command that answers
    "does my change pass this tool". For a formatter, a script that formats is
    preferred over one that only checks, since fixing is the point.
    """
    matches = [
        (command, body)
        for command, body in scripts
        if tool.runs.search(body) and not TRIVIAL_RE.search(body)
    ]
    if tool.checks is not None:
        fixing = [m for m in matches if not re.search(r"\s--check\b", m[1])]
        matches = fixing or matches
    if matches:
        return min(matches, key=lambda m: len(m[1]))[0]
    return tool.command


def _local_sentence(commands: List[str], mentioned: List[str]) -> str:
    if not commands:
        return ""
    if all("`%s`" % c in mentioned for c in commands):
        # CI's own command is the local one; do not print it twice.
        return " Run %s before finishing." % ("it" if len(commands) == 1 else "them")
    return " Run %s before finishing." % _and(["`%s`" % c for c in commands])


def _formats_with_ruff(repo: Repo, expanded, scripts) -> bool:
    pyproject = repo.read("pyproject.toml") or ""
    if re.search(r"^\[tool\.ruff\.format\]", pyproject, re.M):
        return True
    for _command, chain in expanded:
        if any(RUFF_FORMAT_RE.search(text) for text in chain):
            return True
    return any(RUFF_FORMAT_RE.search(body) for _, body in scripts)


def _and(items: List[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _strip_ticks(text: str) -> str:
    return text.replace("`", "")


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
                rule="Python functions carry return type annotations. Match this.",
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
                rule="Python modules and functions carry docstrings. Match this.",
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
        # The formatter's target width, not necessarily a lint error: a
        # project can set it and still ignore E501.
        rule="The configured line length is %s characters (`%s` in `pyproject.toml`)."
        % (match.group(1), match.group(0).strip()),
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
