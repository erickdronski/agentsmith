"""Drift detection: where an existing AGENTS.md disagrees with the repository.

Generating an AGENTS.md is a one-time convenience. Keeping one true is an
ongoing problem, and it is the more valuable of the two — a stale instruction
file is worse than no instruction file, because an agent will follow it
confidently into a wall.

Three classes of problem are reported, in descending severity:

``contradiction``
    The file states something the repository disproves. "Run ``npm install``"
    in a pnpm workspace. These are the ones that actively cause damage, and
    they are the only class that fails the check by default.

``stale``
    The file references a path or script that no longer exists. Usually a
    rename nobody propagated. Harmless in isolation, corrosive in aggregate,
    because it teaches readers the file is unreliable.

``undocumented``
    A convention the repository holds strongly that the file never mentions.
    The weakest signal and the noisiest, so it is informational by default.

False positives are the enemy here. A drift checker that cries wolf gets
removed from CI within a week, so each rule below is deliberately narrow and
declines to guess.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Sequence

from .evidence import Confidence, Finding
from .repo import Repo

__all__ = ["AGENT_FILES", "Drift", "check"]

#: Instruction files worth checking, in the order they are looked for.
AGENT_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/CLAUDE.md",
    ".github/copilot-instructions.md",
    ".cursorrules",
    ".cursor/rules",
)

PACKAGE_MANAGERS = {
    "npm": (re.compile(r"\bnpm (install|ci|run|i)\b"), "package-lock.json"),
    "pnpm": (re.compile(r"\bpnpm (install|run|add|i)\b"), "pnpm-lock.yaml"),
    "yarn": (re.compile(r"\byarn (install|add|run)?\b"), "yarn.lock"),
    "bun": (re.compile(r"\bbun (install|run|add)\b"), "bun.lockb"),
}

#: Looks like a repo path: has a slash or a known source extension, no spaces,
#: no URL scheme, no glob.
PATH_LIKE = re.compile(
    r"^(?!https?:)(?!.*[\s*?])"
    r"(?=.*[/.])"
    r"[\w./@-]+"
    r"(\.\w{1,6}|/)$"
)

SCRIPT_RE = re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+run\s+([\w:.-]+)")


class Drift:
    __slots__ = ("kind", "message", "severity", "source", "suggestion")

    def __init__(
        self,
        kind: str,
        severity: str,
        message: str,
        source: str,
        suggestion: Optional[str] = None,
    ) -> None:
        self.kind = kind
        self.severity = severity
        self.message = message
        self.source = source
        self.suggestion = suggestion

    def to_dict(self) -> Dict[str, str]:
        payload = {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "source": self.source,
        }
        if self.suggestion:
            payload["suggestion"] = self.suggestion
        return payload


def find_agent_file(repo: Repo) -> Optional[str]:
    for candidate in AGENT_FILES:
        if repo.exists(candidate):
            return candidate
    return None


def check(
    repo: Repo,
    findings: Sequence[Finding],
    agent_file: Optional[str] = None,
) -> Dict[str, object]:
    """Compare an instruction file against detected reality."""
    target = agent_file or find_agent_file(repo)
    if target is None:
        return {
            "file": None,
            "drift": [],
            "checked": False,
            "message": (
                "No instruction file found (looked for %s). Run without "
                "--check to generate one." % ", ".join(AGENT_FILES[:3])
            ),
        }

    text = repo.read(target)
    if text is None:
        return {
            "file": target,
            "drift": [],
            "checked": False,
            "message": "Could not read %s" % target,
        }

    prose = _strip_fences_but_keep_commands(text)

    drift: List[Drift] = []
    drift.extend(_package_manager_drift(repo, text, target))
    drift.extend(_stale_paths(repo, text, target))
    drift.extend(_stale_scripts(repo, text, target))
    drift.extend(_undocumented(repo, findings, prose, target))

    order = {"error": 0, "warning": 1, "info": 2}
    drift.sort(key=lambda d: (order.get(d.severity, 3), d.kind, d.message))

    return {
        "file": target,
        "checked": True,
        "drift": [item.to_dict() for item in drift],
        "errors": sum(1 for d in drift if d.severity == "error"),
        "warnings": sum(1 for d in drift if d.severity == "warning"),
        "infos": sum(1 for d in drift if d.severity == "info"),
        "generated_by_agentsmith": "agentsmith:generated" in text,
    }


def _package_manager_drift(repo: Repo, text: str, source: str) -> List[Drift]:
    """The highest-value check: an instruction file naming the wrong tool."""
    actual = None
    for name, (_, lockfile) in PACKAGE_MANAGERS.items():
        if repo.exists(lockfile):
            actual = name
            break
    if actual is None:
        return []

    out: List[Drift] = []
    for name, (pattern, _) in PACKAGE_MANAGERS.items():
        if name == actual:
            continue
        match = pattern.search(text)
        if not match:
            continue
        # `yarn` alone is too loose to act on; require a real subcommand.
        if name == "yarn" and not re.search(r"\byarn (install|add|run)\b", text):
            continue
        out.append(
            Drift(
                kind="contradiction",
                severity="error",
                message=(
                    "%s says to use `%s` (found %r), but this repository's "
                    "lockfile is %s. An agent following this will produce a "
                    "divergent dependency tree."
                    % (source, name, match.group(0), PACKAGE_MANAGERS[actual][1])
                ),
                source=source,
                suggestion="Replace `%s` commands with `%s`." % (name, actual),
            )
        )
    return out


def _stale_paths(repo: Repo, text: str, source: str) -> List[Drift]:
    """Backticked paths in the file that no longer exist on disk."""
    out: List[Drift] = []
    seen = set()

    for raw in re.findall(r"`([^`\n]{2,80})`", text):
        candidate = raw.strip()
        if candidate in seen:
            continue
        seen.add(candidate)

        if " " in candidate or not PATH_LIKE.match(candidate):
            continue
        # Bare filenames with a dot are usually tool names (`package.json` is
        # a path, `Node.js` is not). Require either a slash or a plausible
        # source extension.
        if "/" not in candidate and not re.search(
            r"\.(json|toml|ya?ml|md|ts|tsx|js|jsx|py|go|rs|rb|lock|cfg|ini|txt|sh)$",
            candidate,
        ):
            continue

        probe = candidate.rstrip("/")
        if repo.exists(probe):
            continue
        # A directory the walker excluded still exists; do not report it.
        if os.path.exists(os.path.join(repo.root, probe)):
            continue
        if not _is_repo_rooted(repo, probe):
            continue

        out.append(
            Drift(
                kind="stale",
                severity="warning",
                message="%s references `%s`, which does not exist."
                % (source, candidate),
                source=source,
                suggestion="Update or remove the reference.",
            )
        )

    return out[:20]


def _is_repo_rooted(repo: Repo, probe: str) -> bool:
    """Is this path plausibly meant to be inside *this* repository?

    Instruction files legitimately reference sibling repositories, deploy
    targets, and paths on other machines. Flagging `../other-repo/` or
    `nalee-site/` as "stale" is a false positive, and a drift checker that
    cries wolf gets deleted from CI within a week.

    So a missing path is only reported when it is anchored to something that
    actually exists here: either its first segment is a real top-level entry
    (making it a repo-relative path whose tail has gone stale), or it is a bare
    filename with a source extension, which is unambiguously local.
    """
    if probe.startswith(("..", "/", "~")):
        return False

    segments = probe.split("/")
    if len(segments) == 1:
        # A bare filename. Technology names are written in backticks constantly
        # — `Node.js`, `Next.js`, `Vue.js` — and every one of them ends in a
        # real extension. Requiring lowercase separates `package.json` from
        # `Node.js` without needing a list of framework names to maintain.
        return "." in probe and probe == probe.lower()

    first = segments[0]
    return os.path.exists(os.path.join(repo.root, first))


def _stale_scripts(repo: Repo, text: str, source: str) -> List[Drift]:
    package = repo.read_json("package.json")
    if not package:
        return []
    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        return []

    out: List[Drift] = []
    seen = set()
    for name in SCRIPT_RE.findall(text):
        if name in seen or name in scripts:
            continue
        seen.add(name)
        out.append(
            Drift(
                kind="stale",
                severity="error",
                message=(
                    "%s tells the reader to run `%s`, but package.json defines "
                    "no such script." % (source, name)
                ),
                source=source,
                suggestion="Available scripts: %s" % ", ".join(sorted(scripts)[:10]),
            )
        )
    return out


def _undocumented(
    repo: Repo, findings: Sequence[Finding], prose: str, source: str
) -> List[Drift]:
    """Strong conventions the file never mentions.

    Deliberately conservative: only a handful of keys are checked, each with a
    keyword whose absence really does mean the topic is missing. Reporting
    every undocumented finding would bury the two that matter.
    """
    lowered = prose.lower()
    checks = (
        ("ci-commands", ("ci", "continuous integration", "workflow"), "what CI runs"),
        (
            "test-framework",
            ("test", "vitest", "jest", "pytest", "unittest"),
            "how to run tests",
        ),
        (
            "boundaries",
            ("do not edit", "don't edit", "generated", "migration"),
            "which paths must not be hand-edited",
        ),
        ("commit-convention", ("commit", "conventional"), "commit conventions"),
    )

    available = {f.key for f in findings if Confidence.rank(f.confidence) <= 1}
    out: List[Drift] = []
    for key, keywords, description in checks:
        if key not in available:
            continue
        if any(word in lowered for word in keywords):
            continue
        out.append(
            Drift(
                kind="undocumented",
                severity="info",
                message=(
                    "%s does not mention %s, which this repository has a clear "
                    "convention for." % (source, description)
                ),
                source=source,
                suggestion="Run `agentsmith` to see the detected rule.",
            )
        )
    return out


def _strip_fences_but_keep_commands(text: str) -> str:
    """Remove fenced blocks for prose checks.

    Command drift is checked against the raw text (commands live in fences);
    the "is this topic documented" check runs against prose, so that a stray
    mention inside an unrelated code sample does not count as documentation.
    """
    return re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
