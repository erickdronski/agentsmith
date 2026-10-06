<h1 align="center">agentsmith</h1>

<p align="center"><strong>Your AGENTS.md, mined from what your repo actually does.</strong><br>
No LLM. No network. No config. Evidence for every rule.</p>

<p align="center">
  <a href="#try-it">Try it</a> ·
  <a href="#the-part-that-matters-drift">Drift detection</a> ·
  <a href="#one-set-of-rules-every-agent---target">Claude, Cursor, Copilot</a> ·
  <a href="#what-it-detects">What it detects</a> ·
  <a href="#what-it-refuses-to-do">What it refuses to do</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

<p align="center">
  <img alt="MIT license" src="https://img.shields.io/badge/license-MIT-101828">
  <img alt="zero dependencies" src="https://img.shields.io/badge/dependencies-0-08775c">
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-174ea6">
  <img alt="Linux macOS Windows" src="https://img.shields.io/badge/tested_on-Linux%20%7C%20macOS%20%7C%20Windows-0f766e">
  <img alt="ruff" src="https://img.shields.io/badge/lint-ruff-d97706">
  <img alt="215 tests" src="https://img.shields.io/badge/tests-215-6b21a8">
</p>

---

Everyone hand-writes their `AGENTS.md`. It is accurate for about a week.

Then the test directory moves, someone switches to pnpm, a script gets renamed —
and the file keeps confidently telling your agent to do the old thing. A stale
instruction file is worse than no instruction file, because an agent will follow
it straight into a wall and then explain, at length, why that was reasonable.

`agentsmith` derives the file from the repository instead. Every rule it writes
comes from committed code, configuration, or git history, and carries the count
that produced it.

## Try it

```bash
pip install git+https://github.com/erickdronski/agentsmith
agentsmith                    # print to stdout
agentsmith --out AGENTS.md    # write the file
agentsmith --explain          # include the evidence for every rule
agentsmith --out AGENTS.md --merge   # keep your hand-written sections
agentsmith --target agents,claude,cursor,copilot   # every agent's file, merged
```

Installing from git is the supported path today — this is not on PyPI yet, and
the obvious name there belongs to an unrelated project, so `pip install agentsmith`
would get you someone else's package. When it is published the distribution
name will be `agentsmith-md`.

No API key. No config file. It runs offline in about a second on a large
repository, and it never writes anything you did not ask for.

Real output, from a React Native codebase (an excerpt — Stack, Layout, and
part of the history section are cut for length):

```markdown
## Commands

- Use `npm` for all dependency operations.

- Use the package scripts rather than invoking tools directly:

  - `npm run start` — expo start
  - `npm run test` — vitest run
  - `npm run lint` — expo lint --no-cache
  - `npm run typecheck` — tsc --noEmit

## Verification

- No GitHub Actions workflow here runs on push or pull request — all 9 are scheduled, manually dispatched, or tag-triggered. Do not assume CI will catch a broken change.

## Tests

- Tests run under `vitest`.

- Tests live in a top-level `tests/` directory.

- Test files are named `<name>.test.<ext>`.

- Tests are grouped in `describe()` blocks.

## Code style

- ESLint (linter) is configured in `eslint.config.js`. Run `npm run lint` before finishing.

- String literals use single quotes.

- Statements end with semicolons.

- Indentation is 2 spaces. _(seen in 69% of files)_

## Git and history

- These files change most often, so they are the ones most likely to conflict with concurrent work. Re-read them before editing rather than working from memory:

  - `docs/HANDOFF.md` — changed in 158 of the last 300 commits
  - `src/app/product/[id].tsx` — changed in 30 of the last 300 commits

## Do not edit

- Do not hand-edit these:

  - `package-lock.json` — regenerate through the package manager, never hand-edit
  - `supabase/migrations/` — Applied migrations are immutable — add a new one instead of editing an existing file
```

Note the rule marked _(seen in 69% of files)_. That is a tendency, not a law,
and the file says so. A rule backed by 69% and a rule backed by 100% are
different information, and flattening them into the same confident sentence is
how instruction files start lying.

Note the Verification section, too. This repository has nine workflows, and
version 0.1 took the commands of a manually dispatched release job and wrote
"CI runs these commands. Work is not done until they pass locally" — a
sentence that reads like every other generated rule and was false. Only
workflows that run on push or pull request count as checks now, and when none
do, the file says that instead.

## The part that matters: drift

Generating the file once is a convenience. Keeping it true is the actual
problem.

```bash
agentsmith --check
```

```
Checked AGENTS.md against the repository.

✗ AGENTS.md says to use `npm` (found 'npm install'), but this repository's
  lockfile is pnpm-lock.yaml. An agent following this will produce a divergent
  dependency tree.
    → Replace `npm` commands with `pnpm`.
✗ AGENTS.md tells the reader to run `verify`, but package.json defines no such
  script.
    → Available scripts: build, test
· AGENTS.md does not mention which paths must not be hand-edited, which this
  repository has a clear convention for.

2 contradiction(s), 0 stale reference(s), 1 undocumented convention(s).
```

Exit code is 1 on contradictions, so it drops straight into CI:

```yaml
- run: pip install git+https://github.com/erickdronski/agentsmith
- run: agentsmith --check
```

A ready-made workflow is in
[`.github/workflows/agentsmith.yml`](.github/workflows/agentsmith.yml).

**Precision is the whole design.** A drift checker that cries wolf gets deleted
from CI within a week, taking its true findings with it. So contradictions are
the only class that fails a build; stale references warn; undocumented
conventions are informational. And the checker deliberately declines to guess —
it will not flag `` `Node.js` `` as a missing file, or `` `../sibling-repo/` ``
as a broken path, or the word "yarn" in a sentence about migrating away from it.

A reference is read the way its author meant it, in order: the literal path
first; then, for a bare filename like `` `install.sh` ``, the same name anywhere
in the project; then, for a template like
`` `data/properties/<slug>/facts.toml` `` (or `{name}`, `*`, `**`, `...`,
`NNNN`), a glob. Bracketed names such as `` `src/app/product/[id].tsx` `` are
real files in Next.js and expo-router, so they are only treated as templates
after the literal path fails. A file that genuinely is not there anymore is
still reported. Each of those is a test in
[`tests/test_drift.py`](tests/test_drift.py), and so is the rule that a file
this tool generates must pass this tool's own check.

`--check` reads what the agent actually reads. A `CLAUDE.md` that is just
`@AGENTS.md` is checked through the import (and an import of a missing file is
reported), and `--file .cursor/rules/x.mdc` skips the frontmatter.

## What it detects

| Section | Derived from |
|---------|-------------|
| **Stack** | Dependencies, ecosystem marker files, `requires-python` |
| **Commands** | Lockfiles, `packageManager`, package scripts, workspaces |
| **Verification** | The commands in CI workflows that run on push or pull request — setup steps kept apart from checks, `${{ matrix.* }}` values expanded — plus version matrices and commit hooks |
| **Tests** | Framework config, where test files actually live, how they are named, `describe()` vs bare `test()` |
| **Code style** | Linter, formatter, and type-checker configs, whether CI actually runs them, **and the committed source** — including when the two disagree |
| **Layout** | Top-level structure, file naming per directory, path aliases |
| **Git and history** | Commit conventions with real type and scope lists, subject mood, branch naming, most-churned files |
| **Do not edit** | Migrations, generated output, vendored code, lockfiles, `linguist-generated` |

Two of those are worth calling out.

**Config versus practice.** Most tools read your Prettier config and stop. This
one also reads your source, and reports the gap:

> Prettier is configured (`.prettierrc`), but the committed code does not match
> it: config says semicolons, code omits them in 91% of files. Match the
> committed code and raise the discrepancy — it usually means the formatter is
> not wired into CI.

**Churn as a warning.** The files that change in 60% of commits are the ones an
agent is most likely to conflict on. Nobody writes that down, and it is
computable in one `git log`.

## What it refuses to do

The failure mode that would make this tool useless is not missing a convention.
It is confidently inventing one from four files.

So:

- **Below 8 observations, nothing is asserted.** Ten files agreeing is a
  convention; two files agreeing is a coincidence.
- **Below a 60% majority, nothing is asserted.** A 55/45 split is not a
  convention, it is two conventions, and picking the leader would be a coin flip
  presented as a rule.
- **Nothing generic is ever added.** No "write clean code", no "add tests". If
  the detectors found nothing about testing, there is no Tests section. Padding
  a generated file with filler trains readers to skim it, which destroys the
  value of the specific rules sitting next to the filler.
- **Vendored and generated code is never sampled.** Otherwise a checked-in
  `node_modules` produces an AGENTS.md describing a dependency's house style.
- **Only what git considers part of the project is sampled.** A gitignored
  test-artifacts directory, or a Claude Code worktree under
  `.claude/worktrees/` holding a second copy of the repository, is not the
  project, and counting it once produced rules about another checkout's
  migrations.
- **CI enforcement is never assumed.** "CI will reject this" is only written
  when a workflow that runs on push or pull request is seen running the tool —
  directly, or through `npm run verify` → `npm run lint` → `eslint .` — in a
  step that can fail the build. A configured linter CI never runs is described
  as configured, with the command to run it, and nothing more. A formatter
  only counts as enforced in check mode, because `black .` in CI rewrites files
  and exits 0. A nightly cron job and a manual release button are not merge
  gates, and their commands are never listed as checks.
- **It will not silently overwrite a hand-written file.** If `AGENTS.md` exists
  without the generated marker, you get a warning naming what is being replaced.

## Keep your own writing: `--merge`

The thing that stopped this being adoptable: a team with an existing
`AGENTS.md` full of hard-won architectural knowledge had two options — overwrite
it, or don't use the tool. Everyone picked the second.

```bash
agentsmith --out AGENTS.md --merge
```

Generated rules go inside a marked block. **Everything outside it is never
touched**, on any run, forever:

```markdown
## Architecture
The billing pipeline is eventually consistent. Never assume a write is
readable in the same request.            <- yours, permanently

<!-- agentsmith:begin -->
## Commands
- Use `pnpm` for all dependency operations.   <- regenerated each run
<!-- agentsmith:end -->

## Review notes
Ping @alice on anything touching auth.  <- yours, permanently
```

The split is the point. Mechanical facts — package manager, test layout, CI
commands, generated paths — go stale constantly and are exactly what this tool
derives. Architectural knowledge is what no tool can see, and what is worth
protecting.

A file with no markers keeps all its content and gains the block at the end.
Merging is idempotent, so it is safe on a schedule or in a pre-commit hook. Use
`--dry-run` to see what would change first. If the markers are ever malformed,
it **refuses and writes nothing** rather than guessing which text is yours.

Merged into a file that already has a title, the block adds no title of its
own. A file written earlier by plain `--out` (generated, but without the
markers) gets a warning on merge: its old rules sit outside the block and would
otherwise never be updated again.

## One set of rules, every agent: `--target`

A team is rarely on one tool. Claude Code reads `CLAUDE.md`, Cursor reads
`.cursor/rules/*.mdc`, Copilot reads `.github/copilot-instructions.md` — and
four hand-maintained copies of the same rules drift apart, which is the problem
this tool exists to remove.

```bash
agentsmith --target agents,claude,cursor,copilot --dry-run   # show, write nothing
agentsmith --target agents,claude,cursor,copilot
```

| Target | Writes | What goes in it |
|---|---|---|
| `agents` (default file) | `AGENTS.md` | the generated rules |
| `claude` | `CLAUDE.md` | an `@AGENTS.md` import, so Claude Code reads the same file instead of a copy. Without an AGENTS.md (existing or requested in the same run) the rules go in directly — an import of a missing file would be a broken instruction |
| `cursor` | `.cursor/rules/agentsmith.mdc` | the rules, after `description:` / `alwaysApply: true` frontmatter |
| `copilot` | `.github/copilot-instructions.md` | the rules |

Every target goes through the same marker/merge machinery as `--merge`:
generated content lives between the markers, and **everything outside them is
never touched** — your existing `CLAUDE.md` keeps every line and gains the
import. A `CLAUDE.md` that already imports `AGENTS.md` is left alone. All
targets are planned before any is written, so if one file has malformed
markers the run refuses and **writes nothing at all**. Running it again
changes nothing.

`--target` writes each tool's file at its conventional path inside the analyzed
repository; `--out` writes one file you name, anywhere. They cannot be combined.
To keep the extra files honest in CI, check them by name:

```bash
agentsmith --check --file .cursor/rules/agentsmith.mdc
```

## Use it as a starting point, not an oracle

The generated file is a floor, not a ceiling. It captures what is mechanically
observable — commands, layout, naming, boundaries — which is most of what agents
get wrong and none of what makes your project interesting.

The right workflow is: generate it, then add the things no tool can see. Why the
architecture is the way it is. Which abstractions are load-bearing. What the
last person who touched the payment code wishes they had known. Then keep
`--check` in CI so the mechanical half never rots underneath the hand-written
half.

## Options

```
agentsmith [path]
  --out, -o FILE        write to one file instead of stdout
  --merge               with --out, rewrite only the managed block
  --target TOOL         agents, claude, cursor, copilot; repeatable or
                        comma-separated; always merges; not with --out
  --dry-run             with --out or --target, print what would be written
                        and write nothing
  --format {markdown,json}
  --explain             include evidence for every rule, as collapsible detail
  --check               report drift in an existing instruction file
  --file FILE           which file to check (default: AGENTS.md, CLAUDE.md, ...);
                        @imports are followed, .mdc frontmatter is skipped
  --strict              in --check, also fail on stale and undocumented findings
  --min-confidence {certain,strong,likely,weak}
  --skip DETECTOR       skip a detector; repeatable
```

`--format json` emits every finding with its full evidence, for building your
own tooling on top.

## Testing

```bash
python -m unittest discover -s tests -t .   # 215 tests
```

Detectors are tested against real repositories built on disk, including real git
histories — testing them against mocks would test the mocks. Roughly half the
suite asserts that a detector finds *nothing*, because restraint is the property
that makes the output trustworthy.

## Related

Part of a set of small, standalone tools for working with coding agents:

| Tool | Job |
|---|---|
| [contexttest](https://github.com/erickdronski/contexttest) | A/B tests whether an AGENTS.md change actually helps |
| [burnrate](https://github.com/erickdronski/burnrate) | Prices what your agent sessions cost, with a hard spend cap |
| [tripwire](https://github.com/erickdronski/tripwire) | Audits what your agent is allowed to do |
| [gtm-skills](https://github.com/erickdronski/gtm-skills) | Go-to-market skills for agents, on a tested arithmetic engine |

## License

MIT.
