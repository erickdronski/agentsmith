"""Detector registry.

Each detector is a module exposing ``detect(repo) -> List[Finding]``. They run
independently and are allowed to find nothing — a detector that returns an
empty list for a repository that has no such convention is behaving correctly,
and is far more useful than one that invents a rule to have something to say.

Order here is the order sections appear in the generated file, chosen so an
agent reading top to bottom gets commands before conventions and boundaries
before detail.
"""

from __future__ import annotations

from typing import Callable, List, Sequence, Tuple

from ..evidence import Finding
from ..repo import Repo
from . import history, layout, packaging, style, testing, verification

__all__ = ["DETECTORS", "SECTION_ORDER", "run_all"]

DETECTORS: Tuple[Tuple[str, Callable[[Repo], List[Finding]]], ...] = (
    ("packaging", packaging.detect),
    ("verification", verification.detect),
    ("testing", testing.detect),
    ("style", style.detect),
    ("layout", layout.detect),
    ("history", history.detect),
)

#: Sections in the order they should appear. Anything a detector emits that is
#: not listed here still renders, after these, alphabetically.
SECTION_ORDER = (
    "Stack",
    "Commands",
    "Verification",
    "Tests",
    "Code style",
    "Layout",
    "Git and history",
    "Do not edit",
)


def run_all(repo: Repo, skip: Sequence[str] = ()) -> List[Finding]:
    """Run every detector, collecting findings.

    A detector that raises is skipped rather than aborting the run. Analyzing a
    repository means touching a lot of malformed real-world files, and one
    unparseable config should degrade a section rather than kill the report.
    """
    findings: List[Finding] = []
    for name, detector in DETECTORS:
        if name in skip:
            continue
        try:
            findings.extend(detector(repo) or [])
        except Exception as exc:
            findings.append(
                Finding(
                    key="detector-error-%s" % name,
                    section="Notes",
                    rule=(
                        "The `%s` detector failed on this repository (%s: %s). "
                        "The rest of this file is still valid; that section is "
                        "simply missing." % (name, type(exc).__name__, exc)
                    ),
                    confidence="weak",
                    evidence=[],
                )
            )
    return findings
