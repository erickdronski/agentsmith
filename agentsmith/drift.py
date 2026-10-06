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
from typing import Dict, List, Optional, Pattern, Sequence, Set, Tuple

from .evidence import Confidence, Finding
from .repo import Repo

__all__ = ["AGENT_FILES", "Drift", "check", "imports"]

#: Instruction files worth checking, in the order they are looked for.
AGENT_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/CLAUDE.md",
    ".github/copilot-instructions.md",
    ".cursorrules",
    ".cursor/rules/agentsmith.mdc",
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
    r"[\w./@\[\]-]+"
    r"(\.\w{1,6}|/)$"
)

#: The same shape, but containing placeholders: `data/<slug>/facts.toml`,
#: `src/**/*.test.ts`, `docs/{name}.md`. Requires a slash — a bare `*.test.ts`
#: describes a naming convention, not a file anyone expects to exist.
PLACEHOLDER_PATH_LIKE = re.compile(r"^(?!https?:)[\w./@<>{}*…\[\]-]+$")

#: Placeholder tokens inside one path segment. `[id]` is deliberately absent:
#: Next.js, expo-router, and SvelteKit use literal bracketed names, so a
#: bracketed path is first checked as the real file it usually is.
PLACEHOLDER_TOKEN = re.compile(
    r"<[^<>/]+>|\{[^{}/]+\}|\*"
    r"|(?<![A-Za-z])(?:N{3,}|X{3,}|x{3,}|YYYY(?:-MM(?:-DD)?)?)(?![A-Za-z])"
)

#: Brackets are tried as a placeholder only after the literal reading fails,
#: so `docs/[topic].md` still resolves when it was meant as a template. The
#: cost: a route renamed within the same directory (`[id]` to `[slug]`) is
#: missed. That miss is accepted — the alternative is reporting every
#: bracketed template as a missing file.
BRACKET_TOKEN = re.compile(r"\[[^\[\]/]+\]")

#: A whole segment standing for "some directories here".
ELLIPSIS_SEGMENTS = frozenset({"**", "...", "…"})

SCRIPT_RE = re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+run\s+([\w:.-]+)")

#: Claude Code's `@path` import, restricted to Markdown files. `@types/node`
#: and `@alice` are not imports anyone meant, and treating them as such would
#: report packages and people as missing files.
IMPORT_RE = re.compile(r"(?<![\w`/@])@((?:\.{0,2}/)?[\w.-]+(?:/[\w.-]+)*\.md)\b", re.I)


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
        if os.path.isfile(repo.path(candidate)):
            return candidate
    return None


def check(
    repo: Repo,
    findings: Sequence[Finding],
    agent_file: Optional[str] = None,
) -> Dict[str, object]:
    """Compare an instruction file, and the files it imports, against reality."""
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

    drift: List[Drift] = []
    documents, missing = _with_imports(repo, target, text)
    for path, raw in missing:
        drift.append(
            Drift(
                kind="stale",
                severity="warning",
                message="%s imports `@%s`, which does not exist." % (path, raw),
                source=path,
                suggestion="Update or remove the import.",
            )
        )

    index = _ReferenceIndex(repo)
    for path, body in documents:
        drift.extend(_package_manager_drift(repo, body, path))
        drift.extend(_stale_paths(repo, body, path, index))
        drift.extend(_stale_scripts(repo, body, path))

    # Whether a topic is documented is a question about everything the reader
    # loads, so a CLAUDE.md that only imports AGENTS.md is judged on both.
    prose = _strip_fences_but_keep_commands("\n\n".join(body for _, body in documents))
    drift.extend(_undocumented(repo, findings, prose, target))

    order = {"error": 0, "warning": 1, "info": 2}
    drift.sort(key=lambda d: (order.get(d.severity, 3), d.kind, d.message))

    return {
        "file": target,
        "imports": [path for path, _ in documents[1:]],
        "checked": True,
        "drift": [item.to_dict() for item in drift],
        "errors": sum(1 for d in drift if d.severity == "error"),
        "warnings": sum(1 for d in drift if d.severity == "warning"),
        "infos": sum(1 for d in drift if d.severity == "info"),
        "generated_by_agentsmith": any(
            "agentsmith:generated" in body for _, body in documents
        ),
    }


def imports(text: str) -> List[str]:
    """`@path.md` imports in an instruction file, outside code."""
    body = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    body = re.sub(r"`[^`\n]*`", " ", body)
    return IMPORT_RE.findall(body)


def _with_imports(
    repo: Repo, target: str, text: str
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """The file plus everything it imports, and imports that point nowhere.

    Claude Code follows `@path` imports up to five hops deep. A CLAUDE.md that
    is nothing but `@AGENTS.md` has no rules of its own, so checking it alone
    would find nothing and report the topics as undocumented — while the file
    the reader actually gets has all of them.
    """
    documents = [(target, _strip_frontmatter(text))]
    missing: List[Tuple[str, str]] = []
    seen = {os.path.normpath(target).replace(os.sep, "/")}
    queue = [(target, text, 0)]
    while queue:
        path, body, depth = queue.pop(0)
        if depth >= 5:
            continue
        base = os.path.dirname(path)
        for raw in imports(body):
            if raw.startswith("/"):
                continue  # an absolute path is outside this repository
            resolved = os.path.normpath(os.path.join(base, raw)).replace(os.sep, "/")
            if resolved.startswith("..") or resolved in seen:
                continue
            seen.add(resolved)
            imported = repo.read(resolved)
            if imported is None:
                missing.append((path, raw))
                continue
            documents.append((resolved, _strip_frontmatter(imported)))
            queue.append((resolved, imported, depth + 1))
    return documents, missing


def _strip_frontmatter(text: str) -> str:
    """Drop a leading `---` YAML block, as Cursor `.mdc` rules carry.

    The frontmatter describes the rule file to the editor; its `description:`
    is not an instruction to check, and a glob in `globs:` is not a path.
    Files are read as raw bytes, so a rule saved on Windows arrives with CRLF
    line endings and sometimes a byte-order mark; both are accepted.
    """
    match = re.match(
        r"^\ufeff?---[ \t]*\r?\n.*?\r?\n---[ \t]*(\r?\n|$)", text, re.DOTALL
    )
    return text[match.end() :] if match else text


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


class _ReferenceIndex:
    """Every project file and directory, for asking "does this exist anywhere?".

    Built from :meth:`Repo.all_files`, so in a git repository it is what git
    tracks (plus untracked files that are not ignored), with vendored
    directories excluded either way.
    """

    def __init__(self, repo: Repo) -> None:
        self.files = repo.all_files()
        self.basenames: Set[str] = {path.rsplit("/", 1)[-1] for path in self.files}
        self.directories: Set[str] = set()
        for path in self.files:
            parts = path.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                self.directories.add("/".join(parts[:i]))

    def matches(self, pattern: Pattern[str], directory: bool) -> bool:
        pool = self.directories if directory else self.files
        return any(pattern.match(path) for path in pool)


def _stale_paths(
    repo: Repo, text: str, source: str, index: Optional[_ReferenceIndex] = None
) -> List[Drift]:
    """Backticked paths in the file that no longer exist on disk.

    Three readings, in order, each tried only when the previous one fails:
    the literal path; for a bare filename, the same name anywhere in the
    project ("then run `install.sh`" names a file in `automation/`, it does
    not claim one at the root); and for a path with placeholders, a glob.
    """
    index = index or _ReferenceIndex(repo)
    out: List[Drift] = []
    seen = set()

    for raw in re.findall(r"`([^`\n]{2,80})`", text):
        candidate = raw.strip()
        if candidate in seen:
            continue
        seen.add(candidate)

        if " " in candidate:
            continue
        if _has_placeholder(candidate):
            drift = _stale_pattern(repo, candidate, source, index)
            if drift:
                out.append(drift)
            continue
        if not PATH_LIKE.match(candidate):
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
        if "/" not in probe and probe in index.basenames:
            continue
        if not _is_repo_rooted(repo, probe):
            continue
        if BRACKET_TOKEN.search(probe) and _matches_pattern(probe, index):
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


def _has_placeholder(candidate: str) -> bool:
    segments = candidate.strip("/").split("/")
    return any(
        segment in ELLIPSIS_SEGMENTS or PLACEHOLDER_TOKEN.search(segment)
        for segment in segments
    )


def _stale_pattern(
    repo: Repo, candidate: str, source: str, index: _ReferenceIndex
) -> Optional[Drift]:
    """A path with placeholders that no longer matches any file.

    Only judged when anchored: it needs a slash, and the literal directories
    before the first placeholder must hold project files. `<your-repo>/x`
    and a gitignored `dist/*` are about places this check cannot see, so
    they are left alone rather than reported.
    """
    if "/" not in candidate.strip("/") or not PLACEHOLDER_PATH_LIKE.match(candidate):
        return None
    if candidate.startswith(("..", "/", "~")):
        return None
    segments = candidate.strip("/").split("/")
    prefix: List[str] = []
    for segment in segments:
        if segment in ELLIPSIS_SEGMENTS or PLACEHOLDER_TOKEN.search(segment):
            break
        prefix.append(segment)
    if not prefix or "/".join(prefix) not in index.directories:
        return None

    if _matches_pattern(candidate, index):
        return None
    return Drift(
        kind="stale",
        severity="warning",
        message="%s references `%s`, but no file matches it." % (source, candidate),
        source=source,
        suggestion="Update or remove the reference.",
    )


def _matches_pattern(candidate: str, index: _ReferenceIndex) -> bool:
    """Does any project file or directory match the path read as a template?"""
    segments = candidate.strip("/").split("/")
    for brackets in (False, True):
        if brackets and not BRACKET_TOKEN.search(candidate):
            break
        pattern = _placeholder_regex(segments, brackets)
        if index.matches(pattern, directory=False) or index.matches(
            pattern, directory=True
        ):
            return True
    return False


def _placeholder_regex(segments: List[str], brackets: bool = False) -> Pattern[str]:
    tokens = PLACEHOLDER_TOKEN
    if brackets:
        tokens = re.compile(
            "%s|%s" % (PLACEHOLDER_TOKEN.pattern, BRACKET_TOKEN.pattern)
        )
    parts: List[str] = []
    for i, segment in enumerate(segments):
        last = i == len(segments) - 1
        if segment in ELLIPSIS_SEGMENTS:
            parts.append("(?:[^/]+/)*" + ("[^/]+" if last else ""))
            continue
        found = list(tokens.finditer(segment))
        if not found:
            parts.append(re.escape(segment) + ("" if last else "/"))
            continue
        # A segment with a placeholder is a template for a name, and the
        # words around the placeholder are usually template too:
        # `NNNN-title.md` means "a numbered ADR", not a file whose name ends
        # in "-title". Only the extension after the last placeholder is held
        # fixed — looser than a literal reading, which is the safe direction
        # for a check whose false positives get it removed from CI.
        tail = segment[found[-1].end() :]
        extension = tail[tail.index(".") :] if "." in tail else ""
        parts.append("[^/]*" + re.escape(extension) + ("" if last else "/"))
    return re.compile("^" + "".join(parts) + "$")


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
