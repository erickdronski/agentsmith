"""Package manager, task commands, and the stack a repo actually runs on.

This is the highest-value section of a generated AGENTS.md, because it is the
part an agent gets wrong most expensively. Running ``npm install`` in a pnpm
workspace does not fail loudly — it silently produces a second lockfile and a
different dependency tree, and the damage surfaces later in someone else's
branch.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..evidence import Confidence, Evidence, Finding
from ..repo import Repo

SECTION = "Commands"

#: Lockfile to package manager. Order matters only for reporting; a repo with
#: two lockfiles gets an explicit warning rather than a silent pick.
LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
)

PYTHON_LOCKFILES = (
    ("uv.lock", "uv"),
    ("poetry.lock", "poetry"),
    ("Pipfile.lock", "pipenv"),
    ("pdm.lock", "pdm"),
)

#: Scripts worth surfacing, in the order an agent needs them.
INTERESTING_SCRIPTS = (
    "dev", "start", "build", "test", "lint", "typecheck", "type-check",
    "format", "check", "e2e", "migrate",
)


def detect(repo: Repo) -> List[Finding]:
    findings: List[Finding] = []
    findings.extend(_javascript(repo))
    findings.extend(_python(repo))
    findings.extend(_other_ecosystems(repo))
    return findings


def _javascript(repo: Repo) -> List[Finding]:
    package = repo.read_json("package.json")
    if package is None:
        return []

    findings: List[Finding] = []

    found = [(name, manager) for name, manager in LOCKFILES if repo.exists(name)]

    declared = None
    raw_pm = package.get("packageManager")
    if isinstance(raw_pm, str) and "@" in raw_pm:
        declared = raw_pm.split("@", 1)[0].strip()

    if len(found) > 1:
        names = ", ".join(name for name, _ in found)
        findings.append(
            Finding(
                key="package-manager-conflict",
                section=SECTION,
                rule=(
                    "This repository contains more than one lockfile (%s). "
                    "Confirm which package manager is authoritative before "
                    "installing anything — using the wrong one produces a "
                    "divergent dependency tree that will not fail loudly."
                    % names
                ),
                confidence=Confidence.CERTAIN,
                evidence=[
                    Evidence("lockfiles", "Multiple lockfiles present", samples=[n for n, _ in found])
                ],
            )
        )
    elif found:
        name, manager = found[0]
        evidence = [Evidence(name, "Lockfile present at repository root")]
        rule = "Use `%s` for all dependency operations." % manager
        if declared and declared != manager:
            rule += (
                " Note that package.json declares `packageManager: %s`, which "
                "disagrees with the lockfile — resolve this before installing."
                % raw_pm
            )
            evidence.append(
                Evidence("package.json", "packageManager field says %r" % raw_pm)
            )
        elif declared:
            evidence.append(
                Evidence("package.json", "packageManager field says %r" % raw_pm)
            )
        findings.append(
            Finding(
                key="package-manager",
                section=SECTION,
                rule=rule,
                confidence=Confidence.CERTAIN,
                evidence=evidence,
            )
        )
    elif declared:
        findings.append(
            Finding(
                key="package-manager",
                section=SECTION,
                rule="Use `%s` for all dependency operations." % declared,
                confidence=Confidence.CERTAIN,
                evidence=[
                    Evidence("package.json", "packageManager field says %r" % raw_pm)
                ],
            )
        )

    manager = found[0][1] if found else (declared or "npm")
    runner = "%s run" % manager if manager != "bun" else "bun run"

    scripts = package.get("scripts")
    if isinstance(scripts, dict) and scripts:
        available = [name for name in INTERESTING_SCRIPTS if name in scripts]
        if available:
            lines = [
                "`%s %s` — %s" % (runner, name, _describe_script(name, scripts[name]))
                for name in available
            ]
            findings.append(
                Finding(
                    key="scripts",
                    section=SECTION,
                    rule="Use the package scripts rather than invoking tools "
                    "directly:\n\n" + "\n".join("- " + line for line in lines),
                    confidence=Confidence.CERTAIN,
                    evidence=[
                        Evidence(
                            "package.json",
                            "scripts block defines %d command(s)" % len(scripts),
                            samples=available,
                        )
                    ],
                )
            )

    workspaces = package.get("workspaces")
    if workspaces:
        globs = (
            workspaces if isinstance(workspaces, list)
            else workspaces.get("packages", []) if isinstance(workspaces, dict)
            else []
        )
        findings.append(
            Finding(
                key="workspaces",
                section=SECTION,
                rule=(
                    "This is a monorepo. Install from the repository root, not "
                    "from inside a package — installing in a workspace member "
                    "bypasses hoisting and produces a dependency tree that "
                    "differs from CI."
                ),
                confidence=Confidence.CERTAIN,
                evidence=[
                    Evidence(
                        "package.json",
                        "workspaces declared",
                        samples=[str(g) for g in globs][:5],
                    )
                ],
            )
        )

    stack = _detect_stack(package)
    if stack:
        findings.append(
            Finding(
                key="stack",
                section="Stack",
                rule="Built with %s." % _join(stack),
                confidence=Confidence.CERTAIN,
                evidence=[
                    Evidence("package.json", "dependencies", samples=stack)
                ],
            )
        )

    return findings


def _describe_script(name: str, command: object) -> str:
    text = str(command)
    if len(text) > 90:
        text = text[:87] + "..."
    return text


def _detect_stack(package: dict) -> List[str]:
    deps: Dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        value = package.get(key)
        if isinstance(value, dict):
            deps.update({k: str(v) for k, v in value.items()})

    known = (
        ("next", "Next.js"),
        ("react", "React"),
        ("vue", "Vue"),
        ("svelte", "Svelte"),
        ("@angular/core", "Angular"),
        ("express", "Express"),
        ("fastify", "Fastify"),
        ("nestjs", "NestJS"),
        ("@nestjs/core", "NestJS"),
        ("vite", "Vite"),
        ("webpack", "webpack"),
        ("tailwindcss", "Tailwind CSS"),
        ("prisma", "Prisma"),
        ("drizzle-orm", "Drizzle"),
        ("@supabase/supabase-js", "Supabase"),
        ("expo", "Expo"),
        ("react-native", "React Native"),
        ("electron", "Electron"),
        ("typescript", "TypeScript"),
    )
    return [label for name, label in known if name in deps]


def _python(repo: Repo) -> List[Finding]:
    findings: List[Finding] = []
    pyproject = repo.read("pyproject.toml")
    has_requirements = repo.exists("requirements.txt")

    if not pyproject and not has_requirements and not repo.exists("setup.py"):
        return findings

    found = [(name, tool) for name, tool in PYTHON_LOCKFILES if repo.exists(name)]
    if found:
        name, tool = found[0]
        findings.append(
            Finding(
                key="python-package-manager",
                section=SECTION,
                rule="Use `%s` for Python dependency operations." % tool,
                confidence=Confidence.CERTAIN,
                evidence=[Evidence(name, "Lockfile present")],
            )
        )
    elif has_requirements:
        findings.append(
            Finding(
                key="python-package-manager",
                section=SECTION,
                rule="Python dependencies are pinned in `requirements.txt`; "
                "install with `pip install -r requirements.txt`.",
                confidence=Confidence.CERTAIN,
                evidence=[Evidence("requirements.txt", "Present at root")],
            )
        )
    elif pyproject and "[project]" in pyproject:
        findings.append(
            Finding(
                key="python-package-manager",
                section=SECTION,
                rule="Python packaging is declared in `pyproject.toml` with no "
                "lockfile; dependencies resolve at install time.",
                confidence=Confidence.CERTAIN,
                evidence=[Evidence("pyproject.toml", "[project] table present")],
            )
        )

    if pyproject and "requires-python" in pyproject:
        for line in pyproject.splitlines():
            if line.strip().startswith("requires-python"):
                findings.append(
                    Finding(
                        key="python-version",
                        section="Stack",
                        rule="Python support is constrained: `%s`. Do not use "
                        "syntax newer than the floor." % line.strip(),
                        confidence=Confidence.CERTAIN,
                        evidence=[Evidence("pyproject.toml", line.strip())],
                    )
                )
                break

    return findings


def _other_ecosystems(repo: Repo) -> List[Finding]:
    findings: List[Finding] = []
    markers = (
        ("Cargo.toml", "Rust", "cargo build / cargo test"),
        ("go.mod", "Go", "go build ./... / go test ./..."),
        ("Gemfile", "Ruby", "bundle install / bundle exec rspec"),
        ("pom.xml", "Java (Maven)", "mvn verify"),
        ("build.gradle", "Java/Kotlin (Gradle)", "./gradlew build"),
        ("build.gradle.kts", "Kotlin (Gradle)", "./gradlew build"),
        ("composer.json", "PHP", "composer install"),
        ("Package.swift", "Swift", "swift build / swift test"),
        ("pubspec.yaml", "Dart/Flutter", "flutter test"),
    )
    for filename, language, commands in markers:
        if repo.exists(filename):
            findings.append(
                Finding(
                    key="ecosystem-%s" % filename,
                    section="Stack",
                    rule="%s project (`%s` present). Standard commands: `%s`."
                    % (language, filename, commands),
                    confidence=Confidence.CERTAIN,
                    evidence=[Evidence(filename, "Present at repository root")],
                )
            )
    return findings


def _join(items: List[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]
