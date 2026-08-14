"""Directory structure, file naming, import style, and boundaries.

Two things live here that matter disproportionately to an agent:

**File naming per directory.** Many codebases use different conventions in
different places — ``PascalCase`` for components, ``kebab-case`` for routes,
``snake_case`` for utilities — and a single repo-wide rule would be wrong
everywhere. So naming is detected per directory and reported only where a
directory has enough files to have a convention at all.

**Boundaries.** The paths an agent should not edit: generated output, vendored
code, migrations that have already run. Getting this wrong is among the more
expensive mistakes available, and it is almost never written down.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from ..evidence import Confidence, Evidence, Finding, dominant
from ..repo import Repo

SECTION = "Layout"
BOUNDARY_SECTION = "Do not edit"

CODE_EXTENSIONS = (
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".py",
    ".go",
    ".rb",
    ".rs",
    ".java",
    ".kt",
    ".swift",
    ".php",
    ".mjs",
    ".cjs",
)

#: Directories whose contents are generated, vendored, or otherwise
#: hand-edited only by mistake.
BOUNDARY_MARKERS = (
    (
        "migrations",
        "Applied migrations are immutable — add a new one instead of editing an existing file",
    ),
    ("supabase/migrations", "Applied migrations are immutable — add a new migration"),
    (
        "prisma/migrations",
        "Applied migrations are immutable — change the schema and generate a new migration",
    ),
    ("__generated__", "Generated output — change the generator or the schema instead"),
    ("generated", "Generated output — change the source of generation instead"),
    ("dist", "Build output"),
    ("build", "Build output"),
    (".next", "Framework build output"),
    ("vendor", "Vendored third-party code"),
    ("Pods", "CocoaPods dependencies — edit the Podfile"),
)

LOCKFILES = (
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "poetry.lock",
    "uv.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
)


def detect(repo: Repo) -> List[Finding]:
    findings: List[Finding] = []

    top = _top_level(repo)
    if top:
        findings.append(top)

    findings.extend(_naming_by_directory(repo))

    imports = _import_style(repo)
    if imports:
        findings.append(imports)

    boundaries = _boundaries(repo)
    if boundaries:
        findings.append(boundaries)

    return findings


def _top_level(repo: Repo) -> Optional[Finding]:
    entries = []
    try:
        for name in sorted(os.listdir(repo.root)):
            if name.startswith(".") and name not in (".github",):
                continue
            full = os.path.join(repo.root, name)
            if os.path.isdir(full):
                count = sum(1 for p in repo.files() if p.startswith(name + "/"))
                if count:
                    entries.append((name, count))
    except OSError:
        return None

    if len(entries) < 2:
        return None

    entries.sort(key=lambda item: -item[1])
    listed = "\n".join(
        "- `%s/` — %d file%s" % (name, count, "" if count == 1 else "s")
        for name, count in entries[:12]
    )
    return Finding(
        key="top-level-layout",
        section=SECTION,
        rule="Top-level structure:\n\n" + listed,
        confidence=Confidence.CERTAIN,
        evidence=[
            Evidence(
                "directory listing",
                "%d top-level source directories" % len(entries),
                samples=[name for name, _ in entries[:5]],
            )
        ],
    )


def _classify_name(stem: str) -> Optional[str]:
    if re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)+", stem):
        return "kebab-case"
    if re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)+", stem):
        return "snake_case"
    if re.fullmatch(r"[A-Z][a-zA-Z0-9]*", stem) and re.search(r"[a-z]", stem):
        return "PascalCase"
    if re.fullmatch(r"[a-z][a-zA-Z0-9]*", stem) and re.search(r"[A-Z]", stem):
        return "camelCase"
    if re.fullmatch(r"[a-z0-9]+", stem):
        return None  # Single lowercase word is consistent with several styles.
    return None


def _naming_by_directory(repo: Repo) -> List[Finding]:
    """Detect naming per directory, since conventions vary within a repo."""
    by_dir: Dict[str, Dict[str, int]] = {}
    samples: Dict[str, List[str]] = {}

    for path in repo.files():
        if not path.endswith(CODE_EXTENSIONS):
            continue
        directory = os.path.dirname(path) or "."
        stem = os.path.basename(path)
        for ext in CODE_EXTENSIONS:
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        stem = re.sub(r"\.(test|spec|d|config|stories)$", "", stem)
        style = _classify_name(stem)
        if not style:
            continue
        by_dir.setdefault(directory, {})
        by_dir[directory][style] = by_dir[directory].get(style, 0) + 1
        samples.setdefault(directory, []).append(path)

    # Roll directories up to their first two path segments so a component tree
    # with 40 leaf directories reports once, not forty times.
    rolled: Dict[str, Dict[str, int]] = {}
    rolled_samples: Dict[str, List[str]] = {}
    for directory, counts in by_dir.items():
        parts = directory.split("/")
        key = "/".join(parts[:2]) if len(parts) > 1 else directory
        target = rolled.setdefault(key, {})
        for style, count in counts.items():
            target[style] = target.get(style, 0) + count
        rolled_samples.setdefault(key, []).extend(samples[directory][:3])

    findings: List[Finding] = []
    for directory in sorted(rolled):
        result = dominant(rolled[directory], min_sample=6)
        if not result or result["confidence"] == Confidence.WEAK:
            continue
        findings.append(
            Finding(
                key="naming-%s" % directory.replace("/", "-"),
                section=SECTION,
                rule="Files in `%s/` are named in %s."
                % (directory.rstrip("/"), result["value"]),
                confidence=result["confidence"],
                evidence=[
                    Evidence(
                        "file names under %s/" % directory,
                        "%s naming" % result["value"],
                        observed=result["observed"],
                        total=result["total"],
                        samples=rolled_samples[directory][:4],
                    )
                ],
            )
        )

    # Too many naming rules is noise. Keep the best-evidenced ones.
    findings.sort(key=lambda f: -(f.evidence[0].observed or 0))
    return findings[:6]


def _import_style(repo: Repo) -> Optional[Finding]:
    ts_files = [
        p
        for p in repo.files_matching((".ts", ".tsx", ".js", ".jsx"))
        if ".test." not in p and ".spec." not in p
    ]
    if len(ts_files) < 8:
        return None

    samples = repo.sample_text(ts_files, limit=150)
    if len(samples) < 8:
        return None

    aliased = 0
    relative_deep = 0
    for _, text in samples:
        imports = re.findall(r"""from\s+['"]([^'"]+)['"]""", text)
        if any(spec.startswith(("@/", "~/", "#")) for spec in imports):
            aliased += 1
        if any(spec.startswith("../../") for spec in imports):
            relative_deep += 1

    total = len(samples)
    tsconfig = repo.read_json("tsconfig.json") or {}
    paths = (
        tsconfig.get("compilerOptions", {}).get("paths")
        if isinstance(tsconfig.get("compilerOptions"), dict)
        else None
    )

    if aliased / total >= 0.4 or paths:
        alias_names = sorted(paths.keys())[:5] if isinstance(paths, dict) else []
        rule = (
            "Cross-directory imports use path aliases%s rather than deep "
            "relative paths."
            % (
                " (%s)" % ", ".join("`%s`" % a for a in alias_names)
                if alias_names
                else ""
            )
        )
        if relative_deep:
            rule += (
                " %d of %d sampled files still use `../../` — prefer the alias "
                "in new code." % (relative_deep, total)
            )
        return Finding(
            key="import-style",
            section=SECTION,
            rule=rule,
            confidence=(
                Confidence.CERTAIN
                if paths
                else Confidence.STRONG
                if aliased / total >= 0.7
                else Confidence.LIKELY
            ),
            evidence=[
                Evidence(
                    "tsconfig.json / import statements",
                    "Alias imports present",
                    observed=aliased,
                    total=total,
                )
            ],
        )
    return None


def _boundaries(repo: Repo) -> Optional[Finding]:
    lines: List[str] = []
    samples: List[str] = []

    seen_dirs = set()
    for path in repo.files():
        parts = path.split("/")
        for marker, reason in BOUNDARY_MARKERS:
            marker_parts = marker.split("/")
            for i in range(len(parts) - len(marker_parts) + 1):
                if parts[i : i + len(marker_parts)] == marker_parts:
                    directory = "/".join(parts[: i + len(marker_parts)])
                    if directory not in seen_dirs:
                        seen_dirs.add(directory)
                        lines.append("- `%s/` — %s" % (directory, reason))
                        samples.append(directory)
                    break

    present_locks = [name for name in LOCKFILES if repo.exists(name)]
    if present_locks:
        lines.append(
            "- %s — regenerate through the package manager, never hand-edit"
            % ", ".join("`%s`" % name for name in present_locks)
        )
        samples.extend(present_locks)

    gitattributes = repo.read(".gitattributes") or ""
    for line in gitattributes.splitlines():
        if "linguist-generated" in line:
            pattern = line.split()[0]
            lines.append("- `%s` — marked linguist-generated" % pattern)
            samples.append(pattern)

    if not lines:
        return None

    return Finding(
        key="boundaries",
        section=BOUNDARY_SECTION,
        rule="Do not hand-edit these:\n\n" + "\n".join(sorted(set(lines))),
        confidence=Confidence.CERTAIN,
        evidence=[
            Evidence(
                "directory scan and .gitattributes",
                "Generated, vendored, or immutable paths",
                samples=samples[:5],
            )
        ],
    )
