"""What CI actually runs — the only trustworthy definition of "done".

Every AGENTS.md tells an agent to run the tests. Very few tell it what CI will
actually check, which is the thing that determines whether a change is
mergeable. When the two differ — a typecheck step in CI that no local script
runs — the agent hands back work that fails five minutes later.

So this detector reads the workflow files and extracts the real commands —
from the workflows that run on a change, not from the nightly cron job or the
manual release button (see :mod:`agentsmith.ci` for why that distinction is
the whole point).
"""

from __future__ import annotations

import re
from typing import List, Optional

from ..ci import INTERESTING_RE, gate_commands
from ..evidence import Confidence, Evidence, Finding
from ..repo import Repo

SECTION = "Verification"

HOOK_FILES = (
    (".husky/pre-commit", "Husky pre-commit hook"),
    (".husky/pre-push", "Husky pre-push hook"),
    (".pre-commit-config.yaml", "pre-commit framework"),
    (".githooks/pre-commit", "Repository git hook"),
)

#: Matrix keys that pin a language version, and how to label their values.
VERSION_KEYS = (
    ("python-version", ""),
    ("python", ""),
    ("node-version", "node "),
    ("node", "node "),
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


def _ci_commands(repo: Repo) -> Optional[Finding]:
    scan = gate_commands(repo)
    if not scan.workflows:
        return _other_ci(repo)

    gating = [w for w in scan.workflows if w.gating]
    if not gating:
        return _no_gating_workflow(repo, scan)

    checks = [c for c in scan.commands if c.kind == "check"]
    setup = [c for c in scan.commands if c.kind == "setup"]
    if not checks:
        return None

    listed = "\n".join("- %s" % command.render() for command in checks[:12])
    rule = (
        "CI checks changes with these commands. Work is not done until they "
        "pass locally:\n\n" + listed
    )
    if setup:
        rule += "\n\nBefore the checks, CI sets up with %s." % _and(
            [command.render() for command in setup[:5]]
        )
    matrix = _version_matrix(scan.matrices)
    if matrix:
        rule += "\n\n" + matrix
    opaque = sorted({workflow for workflow, _ in scan.opaque})
    if opaque:
        rule += (
            "\n\nCI also runs steps that are shell scripts or depend on "
            "workflow expressions, so they are not reproduced here — read %s "
            "for those." % _and(["`%s`" % path for path in opaque])
        )

    skipped = [w.path for w in scan.workflows if not w.gating]
    evidence = [
        Evidence(
            "GitHub Actions workflows",
            "%d check(s) and %d setup step(s) from %d workflow(s) that run on "
            "push or pull request" % (len(checks), len(setup), len(gating)),
            samples=[w.path for w in gating][:4],
        )
    ]
    if skipped:
        evidence.append(
            Evidence(
                "GitHub Actions workflows",
                "%d workflow(s) ignored because they only run on a schedule, "
                "manual dispatch, tags, or could not be parsed" % len(skipped),
                samples=skipped[:4],
            )
        )
    return Finding(
        key="ci-commands",
        section=SECTION,
        rule=rule,
        confidence=Confidence.CERTAIN,
        evidence=evidence,
    )


def _no_gating_workflow(repo: Repo, scan) -> Optional[Finding]:
    """Workflows exist, but none of them runs on a change.

    Worth saying, because an agent will otherwise assume CI is a safety net.
    Not said when a workflow could not be parsed — it might have been the gate.
    """
    other = _other_ci(repo)
    if other:
        return other
    if any(not w.parsed for w in scan.workflows):
        return None
    count = len(scan.workflows)
    return Finding(
        key="ci-not-gating",
        section=SECTION,
        rule=(
            "No GitHub Actions workflow here runs on push or pull request — "
            "%s scheduled, manually dispatched, or tag-triggered. Do not "
            "assume CI will catch a broken change."
            % ("it is" if count == 1 else "all %d are" % count)
        ),
        confidence=Confidence.CERTAIN,
        evidence=[
            Evidence(
                "GitHub Actions workflows",
                "Triggers of every workflow file",
                samples=[w.path for w in scan.workflows][:5],
            )
        ],
    )


def _version_matrix(matrices) -> Optional[str]:
    versions = []
    for key, prefix in VERSION_KEYS:
        for value in matrices.get(key) or []:
            if re.match(r"^\d+(\.\d+)*(\.x)?$", value):
                label = prefix + value
                if label not in versions:
                    versions.append(label)
    if not versions:
        return None
    ordered = sorted(versions, key=_version_key)
    return (
        "CI runs a matrix across %s — do not use syntax unavailable on the "
        "oldest of these." % ", ".join(ordered)
    )


def _and(items: List[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


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
        details.append(Evidence(path, label, samples=commands or None))

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
            "Commit hooks are configured (%s). Once installed they can "
            "reformat files or reject a commit — run the formatters and "
            "linters first rather than being surprised by a hook."
            % ", ".join(sorted(set(found)))
        ),
        confidence=Confidence.CERTAIN,
        evidence=details,
    )
