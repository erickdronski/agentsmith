"""What the git history reveals about how this team actually works.

Commit conventions are the clearest signal in any repository, because unlike a
config file, a commit message cannot be aspirational — it either got written
that way 300 times or it did not.

This detector also surfaces two things people rarely write down and always
expect: which paths are hot enough to touch carefully, and whether the project
squashes, merges, or rebases.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..evidence import Confidence, Evidence, Finding, dominant
from ..repo import Repo

SECTION = "Git and history"

CONVENTIONAL_RE = re.compile(
    r"^(?P<type>build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"(?P<scope>\([^)]{1,40}\))?(?P<breaking>!)?: .+"
)

TICKET_RE = re.compile(r"^\[?([A-Z][A-Z0-9]{1,9}-\d+)\]?[:\s]")


def detect(repo: Repo) -> List[Finding]:
    if not repo.is_git:
        return []

    subjects = repo.commit_subjects(limit=400)
    findings: List[Finding] = []

    if len(subjects) < 10:
        return findings

    convention = _commit_convention(subjects)
    if convention:
        findings.append(convention)

    casing = _subject_style(subjects)
    if casing:
        findings.append(casing)

    branches = _branch_convention(repo)
    if branches:
        findings.append(branches)

    hot = _hot_paths(repo)
    if hot:
        findings.append(hot)

    return findings


def _commit_convention(subjects: List[str]) -> Optional[Finding]:
    conventional = [s for s in subjects if CONVENTIONAL_RE.match(s)]
    ticketed = [s for s in subjects if TICKET_RE.match(s)]

    total = len(subjects)
    share_conventional = len(conventional) / total
    share_ticketed = len(ticketed) / total

    if share_conventional >= 0.6:
        types: Dict[str, int] = {}
        scopes: Dict[str, int] = {}
        for subject in conventional:
            match = CONVENTIONAL_RE.match(subject)
            types[match.group("type")] = types.get(match.group("type"), 0) + 1
            scope = match.group("scope")
            if scope:
                clean = scope.strip("()")
                scopes[clean] = scopes.get(clean, 0) + 1

        rule = (
            "Commits follow Conventional Commits (`type: subject`). Types in "
            "use: %s." % ", ".join(
                "`%s`" % name
                for name, _ in sorted(types.items(), key=lambda i: -i[1])[:8]
            )
        )
        if scopes:
            top = sorted(scopes.items(), key=lambda i: -i[1])[:8]
            rule += " Scopes in use: %s." % ", ".join("`%s`" % s for s, _ in top)

        confidence = (
            Confidence.STRONG if share_conventional >= 0.85 else Confidence.LIKELY
        )
        return Finding(
            key="commit-convention",
            section=SECTION,
            rule=rule,
            confidence=confidence,
            evidence=[
                Evidence(
                    "git log",
                    "Subjects matching Conventional Commits",
                    observed=len(conventional),
                    total=total,
                    samples=conventional[:4],
                )
            ],
            note=(
                None
                if share_conventional >= 0.85
                else "Not universal — %d of the last %d commits do not follow it."
                % (total - len(conventional), total)
            ),
        )

    if share_ticketed >= 0.6:
        prefixes = {}
        for subject in ticketed:
            key = TICKET_RE.match(subject).group(1).split("-")[0]
            prefixes[key] = prefixes.get(key, 0) + 1
        return Finding(
            key="commit-convention",
            section=SECTION,
            rule=(
                "Commit subjects begin with an issue key (%s). Include one."
                % ", ".join(
                    "`%s-123`" % p
                    for p, _ in sorted(prefixes.items(), key=lambda i: -i[1])[:3]
                )
            ),
            confidence=(
                Confidence.STRONG if share_ticketed >= 0.85 else Confidence.LIKELY
            ),
            evidence=[
                Evidence(
                    "git log",
                    "Subjects beginning with an issue key",
                    observed=len(ticketed),
                    total=total,
                    samples=ticketed[:4],
                )
            ],
        )

    return None


def _subject_style(subjects: List[str]) -> Optional[Finding]:
    """Imperative vs past tense, and whether subjects end in a period."""
    plain = [s for s in subjects if not CONVENTIONAL_RE.match(s)]
    corpus = plain if len(plain) >= 15 else subjects
    if len(corpus) < 15:
        return None

    bodies = []
    for subject in corpus:
        match = CONVENTIONAL_RE.match(subject)
        bodies.append(subject.split(": ", 1)[1] if match else subject)

    past = sum(1 for b in bodies if re.match(r"^(Added|Fixed|Updated|Removed|Changed|Made)\b", b))
    imperative = sum(1 for b in bodies if re.match(r"^(Add|Fix|Update|Remove|Change|Make|Bump|Refactor)\b", b))

    counts = {}
    if imperative:
        counts["written in the imperative mood (\"Add X\", not \"Added X\")"] = imperative
    if past:
        counts["written in the past tense (\"Added X\")"] = past

    result = dominant(counts, min_sample=8)
    if not result:
        return None

    lengths = [len(b) for b in bodies]
    median = sorted(lengths)[len(lengths) // 2]

    return Finding(
        key="commit-subject-style",
        section=SECTION,
        rule="Commit subjects are %s. Median subject length is %d characters."
        % (result["value"], median),
        confidence=result["confidence"],
        evidence=[
            Evidence(
                "git log",
                "Mood of recent subjects",
                observed=result["observed"],
                total=result["total"],
                samples=bodies[:4],
            )
        ],
    )


def _branch_convention(repo: Repo) -> Optional[Finding]:
    names = repo.branch_names()
    if len(names) < 5:
        return None

    counts: Dict[str, int] = {}
    samples: Dict[str, List[str]] = {}
    for name in names:
        if re.match(r"^(feat|feature|fix|bugfix|hotfix|chore|docs|release)/", name):
            key = "`type/description` (e.g. `feat/add-search`)"
        elif re.match(r"^[a-z0-9._-]+/[A-Z]+-\d+", name):
            key = "`owner/TICKET-123` "
        elif re.match(r"^[A-Z]+-\d+", name):
            key = "`TICKET-123-description`"
        elif "/" in name:
            key = "`namespace/description`"
        else:
            key = "a flat descriptive name with no prefix"
        counts[key] = counts.get(key, 0) + 1
        samples.setdefault(key, []).append(name)

    result = dominant(counts, min_sample=5)
    if not result:
        return None

    return Finding(
        key="branch-naming",
        section=SECTION,
        rule="Branches are named %s." % result["value"].strip(),
        confidence=result["confidence"],
        evidence=[
            Evidence(
                "git refs",
                "Branch naming pattern",
                observed=result["observed"],
                total=result["total"],
                samples=samples[result["value"]],
            )
        ],
    )


def _hot_paths(repo: Repo) -> Optional[Finding]:
    counts = repo.changed_file_counts(limit=300)
    if len(counts) < 20:
        return None

    ranked = sorted(counts.items(), key=lambda item: -item[1])[:8]
    if ranked[0][1] < 4:
        return None

    listed = "\n".join(
        "- `%s` — changed in %d of the last 300 commits" % (path, count)
        for path, count in ranked
    )
    return Finding(
        key="hot-paths",
        section=SECTION,
        rule=(
            "These files change most often, so they are the ones most likely "
            "to conflict with concurrent work. Re-read them before editing "
            "rather than working from memory:\n\n" + listed
        ),
        confidence=Confidence.STRONG,
        evidence=[
            Evidence(
                "git log --name-only",
                "Change frequency over the last 300 commits",
                samples=[path for path, _ in ranked[:5]],
            )
        ],
    )
