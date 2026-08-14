"""Merging generated rules into a file somebody already wrote.

The single thing that stopped this tool from being adoptable: a team with an
existing `AGENTS.md` full of hard-won architectural knowledge had two options,
overwrite it or don't use the tool. Both are bad, and the second is what
everyone picked.

The split is real and worth making explicit. Mechanical facts — which package
manager, where tests live, what CI runs, which paths are generated — go stale
constantly and are exactly what this tool derives. Architectural knowledge —
why the billing pipeline is eventually consistent, which abstraction is
load-bearing, what the last person to touch payments wishes they had known —
is the part no tool can see and the part worth protecting.

So the generated rules live inside a marked block, and everything outside it is
never touched:

    ## Architecture
    The billing pipeline is eventually consistent...   <- yours, forever

    <!-- agentsmith:begin -->
    ## Commands
    - Use `pnpm` for all dependency operations.        <- regenerated
    <!-- agentsmith:end -->

    ## Review notes
    Ping @alice on anything touching auth.             <- yours, forever

A file with no markers keeps all of its content and gains the block at the end.
Nothing is ever deleted from outside the markers, which is the property that
makes the tool safe to run on a schedule.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

__all__ = [
    "BEGIN_MARKER",
    "END_MARKER",
    "MergeError",
    "has_markers",
    "merge",
    "split_managed",
]

BEGIN_MARKER = "<!-- agentsmith:begin -->"
END_MARKER = "<!-- agentsmith:end -->"

_BLOCK_RE = re.compile(
    re.escape(BEGIN_MARKER) + r".*?" + re.escape(END_MARKER),
    re.DOTALL,
)


class MergeError(ValueError):
    """Raised when an existing file cannot be merged into safely."""


def has_markers(text: str) -> bool:
    return BEGIN_MARKER in text and END_MARKER in text


def split_managed(text: str) -> Tuple[str, Optional[str], str]:
    """Split into (before, managed_block_or_None, after).

    Raises rather than guessing when the markers are malformed. A half-open
    block usually means someone edited inside the managed region and clipped a
    marker, and silently picking an interpretation there risks eating their
    text — the exact failure this module exists to prevent.
    """
    begins = text.count(BEGIN_MARKER)
    ends = text.count(END_MARKER)

    if begins == 0 and ends == 0:
        return text, None, ""
    if begins != 1 or ends != 1:
        raise MergeError(
            "expected exactly one agentsmith block, found %d begin and %d end "
            "marker(s). Fix the markers by hand — refusing to guess which text "
            "is managed, because guessing wrong would delete your writing."
            % (begins, ends)
        )

    start = text.index(BEGIN_MARKER)
    end = text.index(END_MARKER)
    if end < start:
        raise MergeError(
            "the agentsmith end marker appears before the begin marker. Fix "
            "the markers by hand."
        )
    return (
        text[:start],
        text[start : end + len(END_MARKER)],
        text[end + len(END_MARKER) :],
    )


def merge(existing: str, generated_body: str) -> str:
    """Return ``existing`` with the managed block replaced by ``generated_body``.

    ``generated_body`` is the rules markdown *without* markers; they are added
    here so callers cannot forget them.
    """
    block = "%s\n%s\n%s" % (BEGIN_MARKER, generated_body.strip(), END_MARKER)

    before, managed, after = split_managed(existing)

    if managed is None:
        # First merge into a hand-written file: keep every word of it and
        # append the managed block. Appending rather than prepending matters —
        # the human's framing should still be the first thing a reader meets.
        separator = (
            ""
            if existing.endswith("\n\n")
            else "\n"
            if existing.endswith("\n")
            else "\n\n"
        )
        if not existing.strip():
            return block + "\n"
        return existing + separator + "\n" + block + "\n"

    return before + block + after


def preview(existing: str, generated_body: str) -> str:
    """Describe what a merge would change, for `--dry-run`."""
    try:
        _before, managed, _after = split_managed(existing)
    except MergeError as exc:
        return "cannot merge: %s" % exc

    preserved = len(
        [ln for ln in _BLOCK_RE.sub("", existing).splitlines() if ln.strip()]
    )
    if managed is None:
        return (
            "would append a managed block and preserve all %d existing "
            "non-empty line(s)" % preserved
        )
    old_lines = len([ln for ln in managed.splitlines() if ln.strip()])
    new_lines = len([ln for ln in generated_body.splitlines() if ln.strip()])
    return (
        "would replace the managed block (%d lines -> %d) and preserve %d "
        "hand-written line(s) outside it" % (old_lines, new_lines, preserved)
    )
