"""What CI actually runs on a change, read from the workflow files.

Two detectors need the same answer. Verification lists the checks a change has
to pass; Code style needs to know whether a configured linter is one of them
before it is allowed to say "CI will reject this". Both used to grep for
``run:`` lines, which could not tell a merge gate from a nightly cron job, a
setup step from a check, or a literal command from one containing
``${{ matrix.browser }}`` — and each of those confusions produced a sentence in
somebody's AGENTS.md that was not true.

So the workflows are parsed (with :mod:`agentsmith.miniyaml`), and only jobs
that run on ``push`` or ``pull_request`` count as CI that checks a change. A
release job on a tag, a scheduled data refresh, and a manually dispatched
TestFlight upload are real automation, but none of them stands between a
change and the main branch, and describing their commands as "work is not done
until these pass" is an invented convention.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .miniyaml import parse
from .repo import Repo

__all__ = [
    "CICommand",
    "Workflow",
    "expand",
    "gate_commands",
    "load_workflows",
    "script_runner",
]

#: Events that run a workflow against a proposed change.
GATE_EVENTS = ("push", "pull_request", "pull_request_target", "merge_group")

#: Commands worth surfacing as checks. Anything else in a gating job is
#: plumbing — `git config`, `mkdir`, `ls` — and listing it would bury the rest.
INTERESTING_RE = re.compile(
    r"(?<![\w./-])("
    r"npm|pnpm|yarn|bun|npx|node|deno|tsc|"
    r"pytest|python\d*(\.\d+)?|ruff|black|mypy|flake8|tox|nox|uv|poetry|hatch|"
    r"pre-commit|"
    r"go\s+(test|build|vet)|cargo\s+(test|build|clippy|fmt)|"
    r"bundle\s+exec|rspec|rubocop|"
    r"gradlew|mvn|swift\s+(test|build)|xcodebuild|"
    r"make|just|task"
    r")(?![\w-])"
)

NOISE_RE = re.compile(
    r"\b(actions/checkout|setup-node|setup-python|(?<!-)cache|upload-artifact|"
    r"download-artifact|codecov|echo)\b"
)

#: Steps that prepare the machine rather than check the change. They are worth
#: knowing — `npm ci` versus `npm install` matters — but an agent does not
#: "pass" `apt-get install`, so they are reported apart from the checks.
SETUP_RE = re.compile(
    r"^(sudo\s+)?("
    r"(python\d*(\.\d+)?\s+-m\s+)?pip3?\s+install\b"
    r"|uv\s+(sync|pip\s+install|tool\s+install|venv)\b"
    r"|(poetry|pipenv|pdm)\s+install\b"
    r"|(bundle|gem)\s+install\b"
    r"|(npx\s+(-y\s+|--yes\s+)?)?playwright\s+install\b"
    r"|apt(-get)?\s+install\b"
    r"|brew\s+install\b"
    r"|corepack\s+(enable|prepare)\b"
    r"|go\s+(mod\s+download|install)\b"
    r"|cargo\s+(fetch|install)\b"
    r"|pre-commit\s+install\b"
    r")"
)

#: Package-manager subcommands that install rather than check.
INSTALL_SUBCOMMANDS = frozenset({"ci", "install", "i", "add", "clean-install"})

#: Flags whose next token is a value, so it is not mistaken for a subcommand.
VALUE_FLAGS = frozenset(
    {"--prefix", "-C", "--cwd", "--dir", "--filter", "-F", "-w", "--workspace"}
)

#: A multi-line `run:` block containing any of these is a program, not a list
#: of commands. Splitting it into lines would lose the control flow — the
#: `cd` that changes what the next line means, the server started in the
#: background — so it is pointed at rather than reproduced.
SHELL_CONTROL_RE = re.compile(
    r"(^|[\s;(])(if|then|elif|else|fi|for|while|until|do|done|case|esac|"
    r"function|trap|exit|return|source|export|cd|pushd|popd|local|read)(?=\s|;|$)"
    r"|\$\(|`|&\s*$|^[A-Za-z_][A-Za-z0-9_]*=|\(\)\s*\{|^[{}]\s*$|<<"
)

#: `node --version`, `python -V`: printing a version checks nothing.
VERSION_PROBE_RE = re.compile(r"^(\S+\s+){0,2}-{1,2}(version|v|V|help)\s*$")

EXPRESSION_RE = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
MATRIX_REF_RE = re.compile(r"^matrix\.([A-Za-z0-9_-]+)$")

#: Job- or step-level `if:` conditions that confine it to a non-gating event.
NON_GATE_CONDITION_RE = re.compile(
    r"github\.event_name\s*==\s*['\"](schedule|workflow_dispatch|release|"
    r"repository_dispatch|workflow_run)['\"]|refs/tags/"
)


class CICommand:
    """One command a gating CI job runs.

    ``kind`` is ``"check"`` or ``"setup"``. ``display`` is the command as it
    should be shown — with matrix expressions replaced by ``<key>`` and the
    values recorded in ``matrix_note`` — while ``raw`` is the original text.
    ``enforcing`` is False when a failure would not fail the build: a step or
    job marked ``continue-on-error``, or a command ending in ``|| true``.
    """

    __slots__ = (
        "display",
        "enforcing",
        "kind",
        "matrix_note",
        "raw",
        "step",
        "workflow",
        "working_directory",
    )

    def __init__(
        self,
        raw: str,
        display: str,
        kind: str,
        workflow: str,
        step: str,
        enforcing: bool,
        working_directory: Optional[str] = None,
        matrix_note: Optional[str] = None,
    ) -> None:
        self.raw = raw
        self.display = display
        self.kind = kind
        self.workflow = workflow
        self.step = step
        self.enforcing = enforcing
        self.working_directory = working_directory
        self.matrix_note = matrix_note

    def render(self) -> str:
        text = "`%s`" % self.display
        directory = (self.working_directory or "").strip("./")
        if directory:
            text += " (in `%s/`)" % directory
        if self.matrix_note:
            text += " — %s" % self.matrix_note
        if self.kind == "check" and not self.enforcing:
            # Listed, because the project runs it; marked, because a reader
            # would otherwise assume a failure here blocks the change.
            text += " (CI ignores its failures)"
        return text


class Workflow:
    """A parsed workflow file, reduced to what the detectors ask about."""

    __slots__ = ("data", "gating", "parsed", "path", "triggers")

    def __init__(self, path: str, data: Any) -> None:
        self.path = path
        self.data = data if isinstance(data, dict) else {}
        self.parsed = isinstance(data, dict) and "jobs" in data
        self.triggers = _triggers(self.data.get("on", self.data.get(True)))
        self.gating = self.parsed and _is_gate(self.triggers)

    def jobs(self) -> List[Tuple[str, Dict[str, Any]]]:
        jobs = self.data.get("jobs")
        if not isinstance(jobs, dict):
            return []
        return [(name, job) for name, job in jobs.items() if isinstance(job, dict)]


class GateScan:
    """Everything the gating workflows run, plus what could not be reproduced."""

    __slots__ = ("commands", "matrices", "opaque", "uses", "workflows")

    def __init__(self) -> None:
        self.workflows: List[Workflow] = []
        self.commands: List[CICommand] = []
        #: (workflow, step name) for steps with an interesting command that is
        #: either a shell program or depends on a non-matrix expression.
        self.opaque: List[Tuple[str, str]] = []
        #: (workflow, step name, action, `with:` inputs, enforcing) for `uses:`.
        self.uses: List[Tuple[str, str, str, Dict[str, Any], bool]] = []
        #: Matrix values per key, across gating jobs, for version ranges.
        self.matrices: Dict[str, List[str]] = {}


def load_workflows(repo: Repo) -> List[Workflow]:
    """Every GitHub Actions workflow file, parsed.

    Listed from the directory rather than from the file index, so a large
    repository can never crowd `.github/` out of the sample.
    """
    directory = repo.path(".github", "workflows")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    out: List[Workflow] = []
    for name in names:
        if not name.endswith((".yml", ".yaml")):
            continue
        relative = ".github/workflows/%s" % name
        text = repo.read(relative)
        if text is None:
            continue
        try:
            data = parse(text)
        except Exception:
            # A workflow this parser cannot read is reported as unparsed rather
            # than guessed at; see Workflow.parsed.
            data = None
        out.append(Workflow(relative, data))
    return out


def gate_commands(repo: Repo) -> GateScan:
    """Commands run by jobs that check a change on push or pull request."""
    scan = GateScan()
    scan.workflows = load_workflows(repo)
    seen = set()

    for workflow in scan.workflows:
        if not workflow.gating:
            continue
        defaults_dir = _working_directory(workflow.data)
        for _job_name, job in workflow.jobs():
            if _confined_to_non_gate(job.get("if")):
                continue
            matrix = _matrix(job)
            for key, values in matrix.items():
                if values:
                    bucket = scan.matrices.setdefault(key, [])
                    bucket.extend(v for v in values if v not in bucket)
            job_tolerant = _truthy(job.get("continue-on-error"))
            job_dir = _working_directory(job) or defaults_dir
            steps = job.get("steps")
            if not isinstance(steps, list):
                continue
            for index, step in enumerate(steps):
                if not isinstance(step, dict) or _confined_to_non_gate(step.get("if")):
                    continue
                name = str(step.get("name") or "step %d" % (index + 1))
                enforcing = not job_tolerant and not _truthy(
                    step.get("continue-on-error")
                )
                if isinstance(step.get("uses"), str):
                    inputs = step.get("with")
                    scan.uses.append(
                        (
                            workflow.path,
                            name,
                            step["uses"],
                            inputs if isinstance(inputs, dict) else {},
                            enforcing,
                        )
                    )
                run = step.get("run")
                if not isinstance(run, str) or not run.strip():
                    continue
                directory = step.get("working-directory") or job_dir
                for command in _step_commands(
                    run, workflow.path, name, matrix, enforcing, directory, scan
                ):
                    key = (command.kind, command.display, command.working_directory)
                    if key in seen:
                        continue
                    seen.add(key)
                    scan.commands.append(command)
    return scan


def _step_commands(
    run: str,
    workflow: str,
    step: str,
    matrix: Dict[str, Optional[List[str]]],
    enforcing: bool,
    directory: Optional[str],
    scan: GateScan,
) -> List[CICommand]:
    lines = _logical_lines(run)
    if len(lines) > 1 and any(SHELL_CONTROL_RE.search(line) for line in lines):
        if any(INTERESTING_RE.search(line) for line in lines):
            scan.opaque.append((workflow, step))
        return []

    out: List[CICommand] = []
    for line in lines:
        kind = classify(line)
        if kind is None:
            continue
        rendered = _render_expressions(line, matrix)
        if rendered is None:
            # `${{ secrets.X }}`, `${{ inputs.y }}`, a matrix built by
            # `fromJSON(...)`: no value a reader could type. Omit the command
            # rather than print a template that cannot be run.
            if kind == "check":
                scan.opaque.append((workflow, step))
            continue
        display, note = rendered
        if len(display) > 120:
            display = display[:117] + "..."
        out.append(
            CICommand(
                raw=line,
                display=display,
                kind=kind,
                workflow=workflow,
                step=step,
                enforcing=enforcing and not _swallows_failure(line),
                working_directory=str(directory) if directory else None,
                matrix_note=note,
            )
        )
    return out


def classify(command: str) -> Optional[str]:
    """``"setup"``, ``"check"``, or ``None`` for plumbing not worth listing."""
    stripped = command.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if SETUP_RE.search(stripped) or _installs_packages(stripped):
        # `apt-get update` and friends are setup, but not worth a line.
        return "setup"
    if NOISE_RE.search(stripped) or not INTERESTING_RE.search(stripped):
        return None
    if VERSION_PROBE_RE.match(stripped):
        return None
    return "check"


def _installs_packages(command: str) -> bool:
    tokens = _tokens(command)
    if tokens and tokens[0] == "sudo":
        tokens = tokens[1:]
    if not tokens or tokens[0] not in ("npm", "pnpm", "yarn", "bun"):
        return False
    subcommand, _ = _subcommand(tokens[1:])
    if subcommand is None:
        return tokens[0] == "yarn"  # bare `yarn` installs
    return subcommand in INSTALL_SUBCOMMANDS


def _logical_lines(run: str) -> List[str]:
    """Split a `run:` script into commands, joining `\\` continuations."""
    out: List[str] = []
    pending = ""
    for raw in run.splitlines():
        line = raw.strip()
        if pending:
            line = pending + " " + line
            pending = ""
        if line.endswith("\\"):
            pending = line[:-1].rstrip()
            continue
        if not line or line.startswith("#") or re.match(r"^set\s+-", line):
            continue
        out.append(line)
    if pending:
        out.append(pending)
    return out


def _render_expressions(
    command: str, matrix: Dict[str, Optional[List[str]]]
) -> Optional[Tuple[str, Optional[str]]]:
    """Expand `${{ matrix.x }}` against literal matrix values, or give up.

    A single literal value is substituted outright. Several become a
    ``<x>`` placeholder plus "for each of a, b, c", which is what the step
    actually does. Any other expression returns ``None``.
    """
    referenced = []
    for expression in EXPRESSION_RE.findall(command):
        ref = MATRIX_REF_RE.match(expression)
        values = matrix.get(ref.group(1)) if ref else None
        if not values:
            return None
        if len(values) > 1 and ref.group(1) not in referenced:
            referenced.append(ref.group(1))

    def substitute(match: "re.Match[str]") -> str:
        key = MATRIX_REF_RE.match(match.group(1)).group(1)
        values = matrix[key] or []
        return values[0] if len(values) == 1 else "<%s>" % key

    rendered = EXPRESSION_RE.sub(substitute, command)
    if len(referenced) == 1:
        note = "for each of %s" % ", ".join(matrix[referenced[0]] or [])
    else:
        note = "; ".join(
            "for each `%s` in %s" % (key, ", ".join(matrix[key] or []))
            for key in referenced
        )
    return rendered, note or None


def _swallows_failure(command: str) -> bool:
    return bool(re.search(r"\|\|\s*(true|:|exit\s+0)\b", command))


# -- workflow structure -----------------------------------------------------


def _triggers(on: Any) -> Dict[str, Any]:
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {str(event): None for event in on}
    if isinstance(on, dict):
        return dict(on)
    return {}


def _is_gate(triggers: Dict[str, Any]) -> bool:
    for event in GATE_EVENTS:
        if event not in triggers:
            continue
        config = triggers[event]
        if event == "push" and isinstance(config, dict):
            # GitHub runs a push workflow with only tag filters for tags
            # alone: that is a release pipeline, not a check on changes.
            keys = set(config)
            if keys & {"tags", "tags-ignore"} and not keys & {
                "branches",
                "branches-ignore",
            }:
                continue
        return True
    return False


def _confined_to_non_gate(condition: Any) -> bool:
    if not isinstance(condition, str):
        return False
    if any(event in condition for event in ('"push"', "'push'", "pull_request")):
        return False
    return bool(NON_GATE_CONDITION_RE.search(condition))


def _matrix(job: Dict[str, Any]) -> Dict[str, Optional[List[str]]]:
    """Literal matrix values per key; ``None`` for a key built by expression."""
    strategy = job.get("strategy")
    if not isinstance(strategy, dict):
        return {}
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict):
        return {}
    values: Dict[str, Optional[List[str]]] = {}
    for key, value in matrix.items():
        if key in ("include", "exclude"):
            continue
        if isinstance(value, list) and all(_literal(v) for v in value):
            values[key] = [str(v) for v in value]
        else:
            values[key] = None
    include = matrix.get("include")
    if isinstance(include, list):
        for entry in include:
            if not isinstance(entry, dict):
                continue
            for key, value in entry.items():
                if not _literal(value):
                    values[key] = None
                    continue
                if key in values and values[key] is None:
                    continue
                bucket = values.setdefault(key, [])
                if bucket is not None and str(value) not in bucket:
                    bucket.append(str(value))
    return values


def _literal(value: Any) -> bool:
    return isinstance(value, str) and "${{" not in value


def _truthy(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == "true"


def _working_directory(node: Dict[str, Any]) -> Optional[str]:
    defaults = node.get("defaults")
    if isinstance(defaults, dict) and isinstance(defaults.get("run"), dict):
        directory = defaults["run"].get("working-directory")
        if isinstance(directory, str) and "${{" not in directory:
            return directory
    return None


# -- following a command through package scripts ----------------------------


def expand(
    repo: Repo, command: str, directory: Optional[str] = None, depth: int = 0
) -> List[str]:
    """The command plus every script body it reaches.

    ``npm run verify`` is opaque on its own; ``verify`` running
    ``npm run lint`` which runs ``eslint .`` is the fact that matters when
    deciding whether CI enforces ESLint. Package scripts (with npm's
    ``pre``/``post`` hooks), Makefile and justfile targets, and pre-commit
    hook ids are followed, to a fixed depth so a cycle terminates.
    """
    out = [command]
    if depth > 6:
        return out
    for segment in re.split(r"&&|\|\||;|\|", command):
        tokens = _tokens(segment)
        if not tokens:
            continue
        for body, where in _resolve(repo, tokens, directory):
            if body in out:
                continue
            out.extend(
                item for item in expand(repo, body, where, depth + 1) if item not in out
            )
    return out


def _resolve(
    repo: Repo, tokens: List[str], directory: Optional[str]
) -> List[Tuple[str, Optional[str]]]:
    tool = tokens[0]
    if tool in ("npm", "pnpm", "yarn", "bun"):
        subcommand, rest = _subcommand(tokens[1:])
        prefix = _flag_value(tokens[1:], ("--prefix", "-C", "--cwd", "--dir"))
        where = _join(directory, prefix)
        scripts = _scripts(repo, where)
        name: Optional[str] = None
        if subcommand in ("run", "run-script") and rest:
            name = rest[0]
        elif subcommand in ("test", "t", "tst", "start", "stop", "restart"):
            name = {"t": "test", "tst": "test"}.get(subcommand, subcommand)
        elif tool != "npm" and subcommand in scripts:
            name = subcommand  # `pnpm lint`, `yarn lint`
        if not name or name not in scripts:
            return []
        names = [name]
        if tool == "npm":
            # npm runs `pre<name>` and `post<name>` automatically; pnpm and
            # yarn berry do not, so only npm gets them.
            names = ["pre" + name, name, "post" + name]
        return [(scripts[n], where) for n in names if n in scripts]
    if tool == "make":
        targets = [t for t in tokens[1:] if not t.startswith("-") and "=" not in t]
        return [(body, directory) for body in _recipes(repo, "Makefile", targets)]
    if tool == "just":
        targets = [t for t in tokens[1:] if not t.startswith("-")][:1]
        return [(body, directory) for body in _recipes(repo, "justfile", targets)]
    if tool == "pre-commit" and len(tokens) > 1 and tokens[1] == "run":
        return [("pre-commit hook " + hook, directory) for hook in hook_ids(repo)]
    return []


def script_runner(repo: Repo) -> str:
    """How this repository's package scripts are run: `npm run`, `pnpm run`..."""
    for lockfile, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("bun.lockb", "bun"),
        ("bun.lock", "bun"),
        ("yarn.lock", "yarn"),
        ("package-lock.json", "npm"),
    ):
        if repo.exists(lockfile):
            return "%s run" % manager
    package = repo.read_json("package.json") or {}
    declared = package.get("packageManager")
    if isinstance(declared, str) and "@" in declared:
        return "%s run" % declared.split("@", 1)[0].strip()
    return "npm run"


def hook_ids(repo: Repo) -> List[str]:
    text = repo.read(".pre-commit-config.yaml") or ""
    return re.findall(r"^\s*-?\s*id:\s*['\"]?([\w.-]+)", text, re.M)


def _scripts(repo: Repo, directory: Optional[str]) -> Dict[str, str]:
    relative = "package.json" if not directory else "%s/package.json" % directory
    package = repo.read_json(relative) or {}
    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {str(k): str(v) for k, v in scripts.items()}


def _recipes(repo: Repo, filename: str, targets: Sequence[str]) -> List[str]:
    """Recipe lines for Makefile or justfile targets, following prerequisites."""
    text = repo.read(filename)
    if not text:
        return []
    rules: Dict[str, Tuple[List[str], List[str]]] = {}
    order: List[str] = []
    current: Optional[List[str]] = None
    for line in text.splitlines():
        header = re.match(r"^([A-Za-z0-9_.\-/ ]+?)\s*:(?!=)\s*(.*)$", line)
        if header and not line[:1].isspace():
            body: List[str] = []
            prerequisites = header.group(2).split("#")[0].split()
            for name in header.group(1).split():
                rules[name] = (prerequisites, body)
                order.append(name)
            current = body
            continue
        if current is not None and line[:1] in ("\t", " ") and line.strip():
            current.append(line.strip().lstrip("@-").strip())
        elif line.strip() and not line[:1].isspace():
            current = None
    wanted = list(targets) or [n for n in order if not n.startswith(".")][:1]
    out: List[str] = []
    visited = set()
    while wanted:
        name = wanted.pop(0)
        if name in visited or name not in rules:
            continue
        visited.add(name)
        prerequisites, body = rules[name]
        wanted.extend(p for p in prerequisites if p in rules)
        out.extend(body)
    return out


def _tokens(segment: str) -> List[str]:
    try:
        return shlex.split(segment.strip())
    except ValueError:
        return segment.split()


def _subcommand(args: List[str]) -> Tuple[Optional[str], List[str]]:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-"):
            if arg in VALUE_FLAGS and "=" not in arg:
                i += 1
            i += 1
            continue
        return arg, args[i + 1 :]
    return None, []


def _flag_value(args: List[str], names: Sequence[str]) -> Optional[str]:
    for i, arg in enumerate(args):
        for name in names:
            if arg == name and i + 1 < len(args):
                return args[i + 1]
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None


def _join(base: Optional[str], extra: Optional[str]) -> Optional[str]:
    parts = [p.strip("/") for p in (base, extra) if p and p.strip("./")]
    return "/".join(parts) or None
