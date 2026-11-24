# Changelog

## [0.3.0] — 2026-11-22

The drift check runs where teams already gate changes — a GitHub Action and a
pre-commit hook — and the rules can be written for Gemini CLI.

### Added

- A composite GitHub Action: `uses: erickdronski/agentsmith@v0` runs
  `agentsmith --check`, with optional `path`, `file`, `strict`, and
  `python-version` inputs. It installs agentsmith from the action's own
  checkout into a private virtualenv, passes inputs to the shell only as
  environment variables, and fails the job on a contradiction. CI runs it on
  this repository and on a generated repository it has to fail.
- A pre-commit hook, `agentsmith-check`, in `.pre-commit-hooks.yaml`. It
  takes no filenames and runs on every commit.
- `--target gemini` writes `GEMINI.md` with an `@./AGENTS.md` import, the form
  Gemini CLI documents, or the rules themselves when there is no AGENTS.md.
  `--check` follows the import, and finds a `GEMINI.md` that is the only
  instruction file.

### Fixed

- `--out` into a directory that did not exist (`--out docs/agents/AGENTS.md`,
  `--out .cursor/rules/x.mdc`) crashed with a FileNotFoundError traceback, and
  `--out` naming a directory with an IsADirectoryError. Missing directories
  are now created, with or without `--merge`, and `--dry-run` names the one it
  would create. A directory, a path through an existing file, or an unwritable
  location exits 2 with one line saying what to fix.
- `--check` exited 0 when the instruction file could not be read. A typo in
  `--file` in a CI configuration passed forever, and so did an AGENTS.md saved
  as UTF-16 (what Windows PowerShell 5.1's `>` writes) in the action and the
  pre-commit hook. It now exits 2 and says why, whether the file was named
  with `--file` or found, and so does an import that exists but cannot be
  read, which was reported as a missing file.
- Each file is read up to its first 120,000 bytes. When that cut fell inside
  a multi-byte character, the whole file was treated as not UTF-8 text; now
  only the cut character is dropped.

### Changed

- The sample workflow, `.github/workflows/agentsmith.yml`, uses the action.
- The release workflow starts only for full version tags (`v*.*.*`), and the
  release procedure in CONTRIBUTING.md moves a floating `v0` tag to each 0.x
  release, the tag `uses: erickdronski/agentsmith@v0` resolves to. Moving it
  does not start another release build.

## [0.2.2] — 2026-11-20

### Fixed

- Check package scripts in the working directory recorded in generated CI
  instructions. Missing scripts now identify the relevant package manifest,
  including in workspaces with their own build and test commands.
- Preserve `CLAUDE.md` symlinks to the local `AGENTS.md` when writing targets,
  including a link whose requested `AGENTS.md` does not exist yet. Previously
  the import could overwrite the generated rules through the link.
- Refuse target paths that resolve outside the repository before writing any
  target. Keep parent-directory imports distinct from the local `AGENTS.md`.

## [0.2.1] — 2026-10-06

### Fixed

- `--check` ignores the frontmatter of a Cursor rule saved with Windows line
  endings or a byte-order mark. The file is read as raw bytes, so a
  `---\r\n` block was never recognised, and its `description:` was checked
  as if it were rules. Caught by the Windows CI job; a new test writes CRLF
  bytes explicitly so the case fails on every platform, not only on Windows.

## [0.2.0] — 2026-10-06

Precision fixes from running 0.1 against real repositories, Markdown that
passes markdownlint, and one set of rules written for every coding agent.

### Fixed: false positives in `--check`

- A bare filename is found anywhere in the project: "then a plain
  `install.sh`" names `automation/install.sh`, not a missing root file
- Placeholder paths match as globs — `<slug>`, `{name}`, `*`, `**`, `...`,
  `NNNN` — so `data/properties/<slug>/facts.toml` resolves; literal
  `[id]` route names are checked as real files before being read as templates
- Naming conventions render as `test_<name>.py`; `test_name.py` was reported
  as a missing file on output the tool had just generated
- Discovery no longer picks the `.cursor/rules` directory as a file

### Fixed: invented or wrong rules

- CI enforcement is claimed only when a push or pull-request workflow is seen
  running the tool, directly or through package scripts, Makefile targets, or
  pre-commit hooks; ESLint is a linter, Black a formatter, mypy a type checker
- Only workflows that run on push or pull request count as checks; scheduled,
  manually dispatched, and tag-only workflows are no longer "work is not done
  until these pass", and a repository with none says so
- Multi-line `run: |` steps are read; shell programs are pointed at rather than
  split into misleading "commands"
- `${{ matrix.x }}` expands against literal matrix values ("for each of
  chromium, firefox, webkit"); other expressions are omitted, not printed
- Setup steps (`npm ci`, `pip install`, `playwright install`, `apt-get
  install`) are listed apart from checks
- `continue-on-error`, `|| true`, and formatters without `--check` never count
  as enforcement
- File sampling uses git's view of the project: ignored directories and
  nested worktrees (`.claude/worktrees/`) no longer produce rules, and no
  longer exhaust the file budget before `.github/` is reached
- Hot paths count the commits actually examined and skip deleted files
- The test runner is inferred by majority across languages (one Python file
  no longer makes a Node project "unittest"); `node:test` and pytest
  configured in `pyproject.toml` or `conftest.py` are recognised
- `tox.ini` alone no longer means Flake8; Python style rules say "Python"
- Analysis inside a git worktree (where `.git` is a file) reads history
- `--dry-run` without `--merge` no longer writes the file
- The drop-in workflow installed `agentsmith` from PyPI, an unrelated package

### Features

- `--target agents,claude,cursor,copilot` writes `AGENTS.md`, `CLAUDE.md`
  (as an `@AGENTS.md` import), `.cursor/rules/agentsmith.mdc`, and
  `.github/copilot-instructions.md` through the merge machinery; all-or-nothing
  on malformed markers; `--dry-run` prints each file
- `--check` follows `@imports` and reports imports of missing files; it reads
  `.mdc` files with frontmatter skipped
- A warning when merging into a file that still holds an earlier full
  generation outside the managed block

### Changed

- Output has single blank lines between blocks (markdownlint MD012), a
  footer without nested emphasis, and no second H1 when merged into a titled
  file; the H1 follows the output file's name
- The line-length rule describes the configured value rather than claiming a
  hard limit

### Tooling

- 215 tests, up from 111; workflows parsed with a dependency-free YAML subset
  parser (`agentsmith/miniyaml.py`)

## [0.1.0] — 2026-08-14

Initial release.

### Detectors

- `packaging` — package manager from lockfiles, script commands, workspaces, stack
- `verification` — real `run:` steps from CI workflows, version matrices, hooks
- `testing` — framework, location, naming, and structure derived from the corpus
- `style` — linter configs plus committed source, including config/practice conflicts
- `layout` — top-level structure, per-directory naming, path aliases, boundaries
- `history` — commit conventions, subject mood, branch naming, churn hot spots

### Features

- `--check` drift detection with severity-based exit codes for CI
- `--explain` to include the evidence behind every rule
- `--format json` for building on top
- Refuses to assert below 8 observations or a 60% majority
- Warns rather than silently overwriting a hand-written instruction file

### Tooling

- 86 tests against real on-disk repositories with real git histories
- CI across Python 3.9–3.13, plus a job that runs the tool on itself
