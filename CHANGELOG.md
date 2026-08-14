# Changelog

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
