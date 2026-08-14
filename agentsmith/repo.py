"""Reading a repository: files, git history, and config, without surprises.

Everything here is read-only and offline. The tool never writes to the target
repository unless you explicitly point ``--out`` at a path, and it never runs a
git command that mutates anything.

Two design decisions worth knowing:

* **Vendored and generated code is excluded from every sample.** A repo with a
  400-file ``node_modules`` checked in would otherwise produce an AGENTS.md
  describing a dependency's house style. The exclusion list is deliberately
  aggressive.
* **File reads are capped and lossy-tolerant.** Detectors sample; they do not
  parse whole codebases. A convention visible in 300 files is visible in 300
  files, and reading 30,000 to confirm it buys nothing but latency.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = ["Repo", "RepoError"]


class RepoError(RuntimeError):
    """Raised when a path cannot be analyzed."""


#: Directories never sampled for style. Anything here is either someone else's
#: code or machine-generated, and both would poison the conventions.
EXCLUDED_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "vendor", "venv", ".venv",
        "env", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        "dist", "build", "out", "target", ".next", ".nuxt", ".svelte-kit",
        "coverage", "htmlcov", ".tox", ".gradle", "Pods", "DerivedData",
        ".terraform", "bower_components", "site-packages", ".cache",
        ".idea", ".vscode", "__snapshots__", ".turbo", ".parcel-cache",
    }
)

#: Files that are generated or vendored even when their directory is not.
GENERATED_PATTERNS = (
    re.compile(r"(^|/)package-lock\.json$"),
    re.compile(r"(^|/)yarn\.lock$"),
    re.compile(r"(^|/)pnpm-lock\.yaml$"),
    re.compile(r"(^|/)poetry\.lock$"),
    re.compile(r"(^|/)Cargo\.lock$"),
    re.compile(r"(^|/)Gemfile\.lock$"),
    re.compile(r"(^|/)composer\.lock$"),
    re.compile(r"\.min\.(js|css)$"),
    re.compile(r"\.(pb|generated)\.(go|ts|js|py)$"),
    re.compile(r"(^|/)__generated__/"),
    re.compile(r"\.d\.ts$"),
)

#: Read at most this much of any single file. Style is visible in the first
#: few hundred lines; the tail is almost never worth the I/O.
MAX_READ_BYTES = 120_000

#: Hard ceiling on files walked, so pointing this at a monorepo terminates.
MAX_FILES = 20_000


class Repo:
    """A read-only view of a repository."""

    def __init__(self, root: str) -> None:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            raise RepoError("not a directory: %s" % root)
        self.root = root
        self._files: Optional[List[str]] = None
        self._git_available: Optional[bool] = None
        self._text_cache: Dict[str, Optional[str]] = {}

    # -- basic filesystem ------------------------------------------------

    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def exists(self, relative: str) -> bool:
        return os.path.exists(self.path(relative))

    def read(self, relative: str) -> Optional[str]:
        """Read a file as text, or ``None`` if missing or undecodable."""
        if relative in self._text_cache:
            return self._text_cache[relative]
        full = self.path(relative)
        text: Optional[str] = None
        try:
            with open(full, "rb") as handle:
                raw = handle.read(MAX_READ_BYTES)
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            text = None
        self._text_cache[relative] = text
        return text

    def read_json(self, relative: str) -> Optional[dict]:
        """Read a JSON file, tolerating JSONC-style comments and trailing commas.

        ``tsconfig.json`` is JSONC in practice, and refusing to read it because
        it has a comment would drop one of the most informative files in a
        TypeScript repo.
        """
        text = self.read(relative)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        stripped = _strip_jsonc(text)
        try:
            value = json.loads(stripped)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None

    # -- file walking ----------------------------------------------------

    def files(self) -> List[str]:
        """All non-excluded files, as repo-relative POSIX paths."""
        if self._files is not None:
            return self._files

        collected: List[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if _walk_into(d)]
            for filename in filenames:
                full = os.path.join(dirpath, filename)
                relative = os.path.relpath(full, self.root).replace(os.sep, "/")
                if _is_generated(relative):
                    continue
                collected.append(relative)
                if len(collected) >= MAX_FILES:
                    self._files = sorted(collected)
                    return self._files
        self._files = sorted(collected)
        return self._files

    def files_matching(
        self, extensions: Sequence[str], limit: Optional[int] = None
    ) -> List[str]:
        """Files with any of the given extensions, e.g. ``(".ts", ".tsx")``."""
        out = [
            path
            for path in self.files()
            if any(path.endswith(ext) for ext in extensions)
        ]
        return out[:limit] if limit else out

    def sample_text(
        self, paths: Iterable[str], limit: int = 200
    ) -> List[Tuple[str, str]]:
        """Read up to ``limit`` files, skipping any that will not decode."""
        out: List[Tuple[str, str]] = []
        for path in paths:
            if len(out) >= limit:
                break
            text = self.read(path)
            if text:
                out.append((path, text))
        return out

    # -- git -------------------------------------------------------------

    @property
    def is_git(self) -> bool:
        return os.path.isdir(os.path.join(self.root, ".git"))

    def git(self, *args: str, timeout: int = 20) -> Optional[str]:
        """Run a read-only git command, returning ``None`` on any failure.

        Failures are common and benign — no git, a shallow clone, an empty
        history — so they degrade the analysis rather than aborting it.
        """
        if not self.is_git:
            return None
        if self._git_available is False:
            return None
        try:
            result = subprocess.run(
                ("git", "-C", self.root) + args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            self._git_available = False
            return None
        self._git_available = True
        if result.returncode != 0:
            return None
        return result.stdout

    def commit_subjects(self, limit: int = 400) -> List[str]:
        """Recent commit subject lines, newest first, merges excluded.

        Merge commits are excluded because their subjects are generated by the
        forge, not written by a human, and including them would make every
        repository look like it uses "Merge pull request #N" as a convention.
        """
        output = self.git(
            "log", "--no-merges", "--pretty=format:%s", "-n", str(limit)
        )
        if not output:
            return []
        return [line.strip() for line in output.splitlines() if line.strip()]

    def branch_names(self, limit: int = 200) -> List[str]:
        output = self.git(
            "for-each-ref", "--format=%(refname:short)", "--count", str(limit),
            "refs/remotes", "refs/heads",
        )
        if not output:
            return []
        names = []
        for line in output.splitlines():
            name = line.strip()
            if not name:
                continue
            if name.startswith("origin/"):
                name = name[len("origin/"):]
            if name in ("HEAD", "main", "master", "develop", "trunk"):
                continue
            names.append(name)
        return names

    def changed_file_counts(self, limit: int = 300) -> Dict[str, int]:
        """How often each path has changed recently — a proxy for hot spots."""
        output = self.git(
            "log", "--no-merges", "--name-only", "--pretty=format:", "-n", str(limit)
        )
        if not output:
            return {}
        counts: Dict[str, int] = {}
        for line in output.splitlines():
            path = line.strip()
            if not path or _is_generated(path):
                continue
            if any(part in EXCLUDED_DIRS for part in path.split("/")):
                continue
            counts[path] = counts.get(path, 0) + 1
        return counts


#: Dot-directories worth descending into. Everything else starting with a dot
#: is tooling state, not source, and sampling it would describe the tools
#: rather than the project.
ALLOWED_DOT_DIRS = frozenset({".github", ".claude", ".config", ".changeset"})


def _walk_into(name: str) -> bool:
    if name in EXCLUDED_DIRS:
        return False
    if name.startswith("."):
        return name in ALLOWED_DOT_DIRS
    return True


def _is_generated(relative: str) -> bool:
    if any(part in EXCLUDED_DIRS for part in relative.split("/")):
        return True
    return any(pattern.search(relative) for pattern in GENERATED_PATTERNS)


def _strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas, respecting strings."""
    out: List[str] = []
    i = 0
    length = len(text)
    in_string = False
    while i < length:
        char = text[i]
        if in_string:
            out.append(char)
            if char == "\\" and i + 1 < length:
                out.append(text[i + 1])
                i += 2
                continue
            if char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "/" and i + 1 < length and text[i + 1] == "/":
            while i < length and text[i] != "\n":
                i += 1
            continue
        if char == "/" and i + 1 < length and text[i + 1] == "*":
            i += 2
            while i + 1 < length and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(char)
        i += 1
    joined = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", joined)
