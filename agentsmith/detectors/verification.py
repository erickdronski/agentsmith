"""What CI actually runs — the only trustworthy definition of "done".

Every AGENTS.md tells an agent to run the tests. Very few tell it what CI will
actually check, which is the thing that determines whether a change is
mergeable. When the two differ — a typecheck step in CI that no local script
runs — the agent hands back work that fails five minutes later.

So this detector reads the workflow files and extracts the real commands.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from ..evidence import Confidence, Evidence, Finding
from ..repo import Repo

SECTION = "Verification"

#: Commands worth surfacing. Checkout, setup, and cache steps are noise.
INTERESTING_RE = re.compile(
    r"\b("
    r"npm|pnpm|yarn|bun|npx|"
    r"pytest|python|ruff|black|mypy|flake8|tox|"
    r"go\s+(test|build|vet)|cargo\s+(test|build|clippy|fmt)|"
    r"bundle\s+exec|rspec|rubocop|"
    r"gradlew|mvn|swift\s+(test|build)|"
    r"make|just|task"
    r")\b"
)

NOISE_RE = re.compile(
    r"\b(actions/checkout|setup-node|setup-python|cache|upload-artifact|"
    r"download-artifact|codecov|echo|apt-get|brew install)\b"
)

HOOK_FILES = (
    (".husky/pre-commit", "Husky pre-commit hook"),
    (".husky/pre-push", "Husky pre-push hook"),
    (".pre-commit-config.yaml", "pre-commit framework"),
    (".githooks/pre-commit", "Repository git hook"),
)


def detect(repo: Repo) -> List[Finding]:
    findings: List[Finding] = []

    ci = _ci_commands(repo)
    if ci:
        findings.append(ci)

    hooks = _hooks(repo)
    if hooks:
        findings.append(hooks)

    return findings


def _workflow_files(repo: Repo) -> List[str]:
    return [
        path
        for path in repo.files()
        if path.startswith(".github/workflows/")
        and path.endswith((".yml", ".yaml"))
    ]


def _ci_commands(repo: Repo) -> Optional[Finding]:
    workflows = _workflow_files(repo)
    if not workflows:
        return _other_ci(repo)

    commands: List[str] = []
    seen = set()
    for path in workflows:
        text = repo.read(path)
        if not text:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line.startswith(("- run:", "run:", "-   run:")):
                continue
            command = line.split("run:", 1)[1].strip()
            command = command.strip("|>-").strip()
            if not command or command in ("|", ">"):
                continue
            if NOISE_RE.search(command) or not INTERESTING_RE.search(command):
                continue
            if len(command) > 120:
                command = command[:117] + "..."
            if command in seen:
                continue
            seen.add(command)
            commands.append(command)

    if not commands:
        return None

    listed = "\n".join("- `%s`" % command for command in commands[:12])
    matrix = _python_matrix(repo, workflows)
    rule = (
        "CI runs these commands. Work is not done until they pass locally:\n\n"
        + listed
    )
    if matrix:
        rule += "\n\n" + matrix

    return Finding(
        key="ci-commands",
        section=SECTION,
        rule=rule,
        confidence=Confidence.CERTAIN,
        evidence=[
            Evidence(
                "GitHub Actions workflows",
                "%d command(s) extracted from %d workflow file(s)"
                % (len(commands), len(workflows)),
                samples=workflows[:4],
            )
        ],
    )


def _python_matrix(repo: Repo, workflows: List[str]) -> Optional[str]:
    versions = set()
    for path in workflows:
        text = repo.read(path) or ""
        for match in re.finditer(
            r"python-version:\s*\[([^\]]+)\]", text
        ):
            for part in match.group(1).split(","):
                cleaned = part.strip().strip("\"'")
                if re.match(r"^\d+\.\d+$", cleaned):
                    versions.add(cleaned)
        for match in re.finditer(r"node-version:\s*\[([^\]]+)\]", text):
            for part in match.group(1).split(","):
                cleaned = part.strip().strip("\"'")
                if re.match(r"^\d+", cleaned):
                    versions.add("node " + cleaned)

    if not versions:
        return None
    ordered = sorted(versions, key=_version_key)
    return (
        "CI runs a matrix across %s — do not use syntax unavailable on the "
        "oldest of these." % ", ".join(ordered)
    )


def _version_key(value: str):
    """Sort version strings numerically.

    Lexical sort puts "3.9" after "3.13", which inverts the answer to the one
    question this line exists to answer — which version is the oldest, and
    therefore what syntax is off limits.
    """
    prefix = ""
    text = value
    if " " in value:
        prefix, _, text = value.partition(" ")
    parts = []
    for chunk in text.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return (prefix, parts)


def _other_ci(repo: Repo) -> Optional[Finding]:
    others = (
        (".gitlab-ci.yml", "GitLab CI"),
        ("Jenkinsfile", "Jenkins"),
        (".circleci/config.yml", "CircleCI"),
        ("azure-pipelines.yml", "Azure Pipelines"),
        (".travis.yml", "Travis CI"),
        ("bitbucket-pipelines.yml", "Bitbucket Pipelines"),
    )
    for filename, name in others:
        if repo.exists(filename):
            return Finding(
                key="ci-commands",
                section=SECTION,
                rule=(
                    "CI runs on %s (`%s`). Read that file to find the commands "
                    "a change has to pass before it can merge." % (name, filename)
                ),
                confidence=Confidence.CERTAIN,
                evidence=[Evidence(filename, "%s configuration" % name)],
            )
    return None


def _hooks(repo: Repo) -> Optional[Finding]:
    found: List[str] = []
    details: List[Evidence] = []

    for path, label in HOOK_FILES:
        text = repo.read(path)
        if text is None:
            continue
        commands = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
            and not line.strip().startswith("#")
            and INTERESTING_RE.search(line)
        ][:4]
        found.append(label)
        details.append(
            Evidence(path, label, samples=commands or None)
        )

    package = repo.read_json("package.json") or {}
    if "lint-staged" in package:
        found.append("lint-staged")
        details.append(Evidence("package.json", "lint-staged configuration"))

    if not found:
        return None

    return Finding(
        key="hooks",
        section=SECTION,
        rule=(
            "Pre-commit tooling is installed (%s). It will reformat or reject "
            "the commit — run the formatter first rather than being surprised "
            "by a hook rewriting your files." % ", ".join(sorted(set(found)))
        ),
        confidence=Confidence.CERTAIN,
        evidence=details,
    )
