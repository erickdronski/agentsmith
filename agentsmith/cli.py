"""Command line interface.

    agentsmith                      # analyze the current directory, print to stdout
    agentsmith --out AGENTS.md      # write the file
    agentsmith --target claude,cursor   # write each tool's file where it looks
    agentsmith --explain            # include the evidence for every rule
    agentsmith --check              # compare an existing file against reality
    agentsmith --format json        # machine-readable findings

Exit codes are chosen so this is useful in CI:

``0``
    Clean. In ``--check`` mode this means no contradictions were found.
``1``
    Drift found that should fail a build (contradictions, and stale script
    references, which are contradictions wearing a different hat).
``2``
    Could not run — bad path, unreadable repository, conflicting options, or
    a file that cannot be merged into safely (in which case nothing is
    written).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional, Sequence

from . import __version__
from .detectors import DETECTORS, run_all
from .drift import AGENT_FILES
from .drift import check as check_drift
from .evidence import Confidence, sort_findings
from .merge import MergeError, merge, preview, split_managed
from .render import GENERATED_MARKER, render_json, render_markdown
from .repo import Repo, RepoError
from .targets import TARGETS, legacy_warning, parse_targets, plan

__all__ = ["main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentsmith",
        description=(
            "Mine a repository's actual conventions into an AGENTS.md, with "
            "evidence for every rule. No LLM, no network, no config."
        ),
        epilog=(
            "Every rule in the output was derived from committed code, "
            "configuration, or git history. Run with --explain to see exactly "
            "what produced each one."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="repository to analyze (default: current directory)",
    )
    parser.add_argument(
        "--out",
        "-o",
        help="write to this one file instead of stdout",
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        metavar="TOOL",
        help=(
            "write the rules where a coding agent reads them: %s (repeatable "
            "or comma-separated). Each file is merged — hand-written text "
            "outside the managed block is kept. Cannot be combined with --out"
            % ", ".join("%s (%s)" % (name, path) for name, path in TARGETS)
        ),
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="output format (default: markdown)",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help=(
            "update only the managed block in --out, preserving every "
            "hand-written line outside it (creates the block on first run). "
            "--target always merges"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "with --out or --target, describe what would change and print the "
            "resulting file(s) to stdout, without writing anything"
        ),
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="include the evidence behind every rule, as collapsible detail",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "compare an existing instruction file against the repository and "
            "report drift; exits non-zero on contradictions"
        ),
    )
    parser.add_argument(
        "--file",
        help=(
            "instruction file for --check, e.g. .cursor/rules/x.mdc (default: "
            "first of %s that exists). @imports are followed"
            % ", ".join(AGENT_FILES[:3])
        ),
    )
    parser.add_argument(
        "--min-confidence",
        choices=Confidence.ORDER,
        default=Confidence.WEAK,
        help=("drop rules weaker than this (default: weak, i.e. keep everything)"),
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="DETECTOR",
        choices=[name for name, _ in DETECTORS],
        help="skip a detector; repeatable (%s)"
        % ", ".join(name for name, _ in DETECTORS),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="in --check mode, also fail on stale references and undocumented conventions",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="agentsmith %s" % __version__,
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    targets: List[str] = []
    if args.target:
        try:
            targets = parse_targets(args.target)
        except ValueError as exc:
            return _usage_error(str(exc))
        if args.out:
            return _usage_error(
                "--out and --target cannot be combined. --target writes each "
                "tool's file at its conventional path inside the repository; "
                "--out writes one file you name."
            )
        if args.format == "json":
            return _usage_error(
                "--target writes Markdown instruction files and cannot be "
                "combined with --format json."
            )
        if args.check:
            return _usage_error(
                "--check reads one instruction file; choose it with --file "
                "rather than --target."
            )

    try:
        repo = Repo(args.path)
    except RepoError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2

    findings = sort_findings(run_all(repo, skip=args.skip))
    repo_name = os.path.basename(repo.root)

    if args.check:
        return _run_check(repo, findings, args)

    if targets:
        return _write_targets(repo, findings, args, targets)

    target = None
    if args.out:
        target = (
            args.out if os.path.isabs(args.out) else os.path.join(os.getcwd(), args.out)
        )
        if args.merge:
            # The non-destructive path: only the managed block is rewritten, so
            # there is nothing to warn about.
            return _write_merged(repo, target, args, findings)

    if args.format == "json":
        output = render_json(findings, repo_name, __version__)
    else:
        output = render_markdown(
            findings,
            repo_name,
            explain=args.explain,
            min_confidence=args.min_confidence,
            title=os.path.basename(target) if target else "AGENTS.md",
            check_command=_check_command(repo, target),
        )

    if target:
        if args.dry_run:
            sys.stderr.write("%s: %s\n" % (args.out, _describe_overwrite(target)))
            sys.stdout.write(output.rstrip("\n") + "\n")
            return 0
        _warn_on_overwrite(target)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(output.rstrip("\n") + "\n")
        sys.stderr.write(
            "wrote %s — %d rule(s) from %d detector(s)\n"
            % (args.out, len(findings), len(DETECTORS) - len(args.skip))
        )
    else:
        sys.stdout.write(output.rstrip("\n") + "\n")

    if not findings:
        sys.stderr.write(
            "note: no conventions detected with confidence. Very small, very "
            "new, or genuinely inconsistent repositories are better served by "
            "a hand-written file.\n"
        )
    return 0


def _usage_error(message: str) -> int:
    sys.stderr.write("error: %s\n" % message)
    return 2


def _check_command(repo: Repo, target: Optional[str]) -> str:
    """The `--check` invocation that would actually read this file."""
    if not target:
        return "agentsmith --check"
    relative = os.path.relpath(target, repo.root).replace(os.sep, "/")
    if relative.startswith("..") or relative in AGENT_FILES[:1]:
        return "agentsmith --check"
    return "agentsmith --check --file %s" % relative


def _write_targets(repo: Repo, findings, args, targets: List[str]) -> int:
    """Write (or with --dry-run, show) every requested target, or none."""
    repo_name = os.path.basename(repo.root)

    def render(title: Optional[str], check_command: str) -> str:
        return render_markdown(
            findings,
            repo_name,
            explain=args.explain,
            min_confidence=args.min_confidence,
            title=title,
            check_command=check_command,
        )

    plans = plan(repo.root, targets, render)
    failed = [item for item in plans if item.error]
    if failed:
        for item in failed:
            sys.stderr.write("error: %s: %s\n" % (item.path, item.error))
        sys.stderr.write("nothing was written.\n")
        return 2

    for item in plans:
        if args.dry_run:
            sys.stderr.write("%s: %s\n" % (item.path, item.summary))
            if item.content is not None:
                sys.stdout.write(
                    "==> %s <==\n%s\n" % (item.path, item.content.rstrip("\n"))
                )
        elif item.content is None:
            sys.stderr.write("%s: %s\n" % (item.path, item.summary))
        else:
            full = os.path.join(repo.root, item.path)
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(item.content.rstrip("\n") + "\n")
            preserved = _preserved_lines(item.existing or "")
            sys.stderr.write(
                "%s %s%s\n"
                % (
                    "created" if item.existing is None else "merged into",
                    item.path,
                    "; %d existing line(s) preserved" % preserved if preserved else "",
                )
            )
        if item.note:
            sys.stderr.write("note: %s\n" % item.note)
        if item.warning:
            sys.stderr.write("warning: %s\n" % item.warning)
    return 0


def _preserved_lines(existing: str) -> int:
    """Non-empty lines outside the managed block, i.e. the human's."""
    try:
        before, _managed, after = split_managed(existing)
    except MergeError:
        return 0
    return len([ln for ln in (before + after).splitlines() if ln.strip()])


def _write_merged(repo: Repo, target: str, args, findings) -> int:
    """Update only the managed block, preserving everything a human wrote."""
    existing = ""
    if os.path.exists(target):
        try:
            with open(target, encoding="utf-8") as handle:
                existing = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            sys.stderr.write("error: could not read %s (%s)\n" % (target, exc))
            return 2

    try:
        before, _managed, after = split_managed(existing)
    except MergeError as exc:
        if args.dry_run:
            sys.stderr.write("%s: cannot merge: %s\n" % (args.out, exc))
            return 0
        sys.stderr.write("error: %s\n" % exc)
        return 2

    # The rules are a section of someone else's document when there is text
    # outside the block, and must not add a second title to it.
    title = None if (before + after).strip() else os.path.basename(target)
    output = render_markdown(
        findings,
        os.path.basename(repo.root),
        explain=args.explain,
        min_confidence=args.min_confidence,
        title=title,
        check_command=_check_command(repo, target),
    )
    merged = merge(existing, output)
    warning = legacy_warning(existing, args.out)

    if args.dry_run:
        sys.stderr.write("%s: %s\n" % (args.out, preview(existing, output)))
        if warning:
            sys.stderr.write("warning: %s\n" % warning)
        sys.stdout.write(merged.rstrip("\n") + "\n")
        return 0

    with open(target, "w", encoding="utf-8") as handle:
        handle.write(merged.rstrip("\n") + "\n")

    preserved = _preserved_lines(existing) if existing else 0
    sys.stderr.write(
        "merged into %s — %d rule(s) in the managed block%s\n"
        % (
            args.out,
            len(findings),
            "; %d existing line(s) preserved" % preserved if preserved else "",
        )
    )
    if warning:
        sys.stderr.write("warning: %s\n" % warning)
    return 0


def _describe_overwrite(target: str) -> str:
    if not os.path.exists(target):
        return "would create it"
    try:
        with open(target, encoding="utf-8") as handle:
            existing = handle.read(4000)
    except (OSError, UnicodeDecodeError):
        existing = ""
    if GENERATED_MARKER in existing:
        return "would replace it (a previous agentsmith output)"
    return (
        "would replace it — it was not generated by agentsmith, so its "
        "contents would be lost (use --merge to keep them)"
    )


def _warn_on_overwrite(target: str) -> None:
    """Never silently clobber a hand-written instruction file."""
    if not os.path.exists(target):
        return
    try:
        with open(target, "r", encoding="utf-8") as handle:
            existing = handle.read(4000)
    except (OSError, UnicodeDecodeError):
        return
    if GENERATED_MARKER in existing:
        return
    sys.stderr.write(
        "warning: %s exists and was not generated by agentsmith. Its contents "
        "are being replaced — recover them from git if that was not intended.\n"
        % os.path.basename(target)
    )


def _run_check(repo: Repo, findings, args) -> int:
    result = check_drift(repo, findings, agent_file=args.file)

    if args.format == "json":
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
    else:
        sys.stdout.write(_format_check(result) + "\n")

    if not result.get("checked"):
        return 0

    failing = result["errors"]
    if args.strict:
        failing += result["warnings"] + result["infos"]
    return 1 if failing else 0


def _format_check(result: dict) -> str:
    if not result.get("checked"):
        return str(result.get("message", "nothing to check"))

    lines: List[str] = []
    target = result["file"]
    if result.get("imports"):
        target = "%s (and %s, which it imports)" % (
            target,
            ", ".join(result["imports"]),
        )
    drift = result["drift"]

    if not drift:
        lines.append("%s matches the repository. No drift found." % target)
        return "\n".join(lines)

    symbols = {"error": "✗", "warning": "!", "info": "·"}
    lines.append("Checked %s against the repository.\n" % target)

    for item in drift:
        lines.append("%s %s" % (symbols.get(item["severity"], "·"), item["message"]))
        if item.get("suggestion"):
            lines.append("    → %s" % item["suggestion"])
    lines.append("")

    lines.append(
        "%d contradiction(s), %d stale reference(s), %d undocumented "
        "convention(s)." % (result["errors"], result["warnings"], result["infos"])
    )
    if result["errors"]:
        lines.append(
            "\nContradictions are the ones that cause damage: an agent will "
            "follow them confidently into a wall."
        )
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
