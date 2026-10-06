# Changelog

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
