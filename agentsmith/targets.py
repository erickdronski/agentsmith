"""Writing the rules where each coding agent looks for them.

AGENTS.md is read by Codex, Cursor, Copilot's coding agent, and others, but a
team is rarely on one tool. Claude Code reads CLAUDE.md, Cursor reads
`.cursor/rules/*.mdc`, Copilot Chat reads `.github/copilot-instructions.md`.
Generating each of those separately would mean four copies of the same rules
drifting apart — the exact problem this tool exists to remove.

So every target goes through the same marker/merge machinery as ``--merge``:
generated rules live between the markers, and everything outside them is
never touched. And where a tool can import a file instead of duplicating it —
Claude Code resolves ``@AGENTS.md`` — the target is an import, not a copy.
"""

from __future__ import annotations

import os
from typing import Callable, List, Optional, Sequence

from .drift import imports
from .merge import MergeError, has_markers, merge, preview, split_managed
from .render import GENERATED_MARKER

__all__ = ["TARGETS", "Plan", "parse_targets", "plan"]

#: Target name -> conventional path, in the order they are written. AGENTS.md
#: comes first so that a CLAUDE.md written in the same run can import it.
TARGETS = (
    ("agents", "AGENTS.md"),
    ("claude", "CLAUDE.md"),
    ("cursor", ".cursor/rules/agentsmith.mdc"),
    ("copilot", ".github/copilot-instructions.md"),
)

TITLES = {
    "agents": "AGENTS.md",
    "claude": "CLAUDE.md",
    "cursor": "Repository conventions",
    "copilot": "Copilot instructions",
}

#: Cursor only applies a rule file whose frontmatter says when to. An
#: always-on rule needs no `globs`.
CURSOR_FRONTMATTER = """---
description: Repository conventions derived by agentsmith from the code, configuration, and git history
alwaysApply: true
---
"""

CLAUDE_IMPORT = """Rules for this repository live in AGENTS.md, which agentsmith derives from
the code and checks for drift. Claude Code reads them through this import:

@AGENTS.md"""

#: (title, check command) -> rendered rules markdown.
Renderer = Callable[[Optional[str], str], str]


class Plan:
    """What one target would do: the content to write, or why not."""

    __slots__ = (
        "content",
        "error",
        "existing",
        "name",
        "note",
        "path",
        "summary",
        "warning",
    )

    def __init__(self, name: str, path: str) -> None:
        self.name = name
        self.path = path
        self.existing: Optional[str] = None
        self.content: Optional[str] = None  # None: nothing to write
        self.summary = ""
        self.note: Optional[str] = None
        self.warning: Optional[str] = None
        #: Set when this target must not be written. Any error stops the run.
        self.error: Optional[str] = None


def parse_targets(values: Sequence[str]) -> List[str]:
    """``["claude,cursor", "agents"]`` -> canonical order, deduplicated."""
    known = [name for name, _ in TARGETS]
    requested = set()
    for value in values:
        for part in value.split(","):
            name = part.strip().lower()
            if not name:
                continue
            if name not in known:
                raise ValueError(
                    "unknown target %r (choose from %s)" % (name, ", ".join(known))
                )
            requested.add(name)
    return [name for name in known if name in requested]


def plan(root: str, names: Sequence[str], render: Renderer) -> List[Plan]:
    """Work out every target's new content without writing anything.

    Planning everything first means a malformed file in one target stops the
    whole run before any file is written, rather than leaving the repository
    half-updated.
    """
    plans: List[Plan] = []
    for name, path in TARGETS:
        if name not in names:
            continue
        item = Plan(name, path)
        full = os.path.join(root, path)
        if os.path.exists(full):
            item.existing = _read(full)
            if item.existing is None:
                item.error = "%s exists but could not be read as UTF-8 text" % path
                plans.append(item)
                continue
        if name == "claude":
            agents_available = "agents" in names or os.path.isfile(
                os.path.join(root, "AGENTS.md")
            )
            _plan_claude(item, render, agents_available)
        else:
            _plan_rules(item, render)
        plans.append(item)
    return plans


def _plan_rules(item: Plan, render: Renderer) -> None:
    check = "agentsmith --check"
    if item.name in ("cursor", "copilot"):
        check = "agentsmith --check --file %s" % item.path
    existing = item.existing
    if existing is None and item.name == "cursor":
        existing = CURSOR_FRONTMATTER
    body = render(_title(item.name, existing), check)
    _merge_into(item, existing or "", body)


def _plan_claude(item: Plan, render: Renderer, agents_available: bool) -> None:
    existing = item.existing or ""
    if not agents_available:
        # Importing a file that will not exist is worse than duplicating, so
        # the rules go into CLAUDE.md itself.
        body = render(_title("claude", existing), "agentsmith --check")
        _merge_into(item, existing, body)
        item.note = (
            "AGENTS.md does not exist and was not requested, so the rules are "
            "written into CLAUDE.md directly (add --target agents to keep one "
            "copy and import it)"
        )
        return

    outside = existing
    if has_markers(existing):
        try:
            before, _managed, after = split_managed(existing)
            outside = before + after
        except MergeError as exc:
            item.error = str(exc)
            return
    if any(_is_agents(path) for path in imports(outside)):
        item.summary = "already imports AGENTS.md; left unchanged"
        return

    body = CLAUDE_IMPORT
    if not outside.strip():
        body = "# CLAUDE.md\n\n" + body
    _merge_into(item, existing, body)


def _merge_into(item: Plan, existing: str, body: str) -> None:
    try:
        merged = merge(existing, body)
    except MergeError as exc:
        item.error = str(exc)
        return
    if item.existing is None:
        item.summary = "would create"
    else:
        item.summary = preview(existing, body)
        if merged == existing:
            item.summary = "already up to date"
    item.content = merged if merged != item.existing else None
    item.warning = legacy_warning(existing, item.path)


def legacy_warning(existing: str, path: str) -> Optional[str]:
    """Warn when a previous full generation sits outside the managed block.

    A file written by plain ``--out`` has the generated marker but no
    begin/end markers. Merging into it keeps every line, as promised — which
    means the old rules stay too, outside the block, never updated again.
    Deleting them would break the promise; saying nothing would leave stale
    rules in the file forever. So say it.
    """
    if GENERATED_MARKER not in existing:
        return None
    try:
        before, _managed, after = split_managed(existing)
    except MergeError:
        return None
    if GENERATED_MARKER in before + after:
        return (
            "%s contains rules from an earlier full generation outside the "
            "managed block; they will not be updated. Delete everything outside "
            "the agentsmith markers that you did not write yourself." % path
        )
    return None


def _title(name: str, existing: Optional[str]) -> Optional[str]:
    """An H1 only when the rules are the whole file.

    Merged into a file that already has content (or Cursor's frontmatter),
    the block is a section of someone else's document and must not add a
    second title to it.
    """
    if existing and existing.strip():
        try:
            before, _managed, after = split_managed(existing)
        except MergeError:
            return None
        if (before + after).strip():
            return None
    return TITLES[name] if name != "cursor" else None


def _is_agents(path: str) -> bool:
    return os.path.normpath(path).replace(os.sep, "/").lstrip("./") == "AGENTS.md"


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None
