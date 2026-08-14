# Contributing

New detectors are the most valuable contribution. So are false-positive reports.

## The bar

**A detector must be able to find nothing.** The failure mode that would make
this tool useless is not missing a convention — it is confidently inventing one
from four files. Every detector needs at least one test proving it stays silent
on a repository that has no such convention.

**Evidence, always.** A detector never emits a bare instruction. It emits a
`Finding` carrying the rule, the evidence, and a confidence derived from how
dominant the pattern actually is. If you cannot count it, you cannot assert it.

**Use `dominant()`.** Do not hand-roll majority logic. It encodes the minimum
sample size and the confidence thresholds in one place, and those thresholds are
the reason the output is trustworthy.

**No dependencies.** Standard library only; CI fails if a `requirements.txt`
appears. This installs anywhere Python runs, with no supply chain.

**Python 3.9 compatible.** No `X | Y` unions, no `match`.

## Adding a detector

1. Create `agentsmith/detectors/<name>.py` exposing `detect(repo) -> List[Finding]`.
2. Register it in `agentsmith/detectors/__init__.py`, in the order its section
   should appear.
3. Add its section to `SECTION_ORDER` if it is new.
4. Test it against a real fixture repository — see `tests/fixtures.py`, which
   builds actual directories and actual git histories.

```bash
python -m unittest discover -s tests -t .
python -m agentsmith .          # dogfood it
python -m agentsmith . --check
```

## Reporting a false positive

This is the most useful bug report you can file. Include the repository shape
that triggered it — the file layout, the config, the doc line — and what the
tool said. Precision is the design constraint here: a checker that cries wolf
gets deleted from CI within a week, taking its true findings with it.

Every past false positive is pinned by a test. `Node.js` read as a missing file,
a sibling repository flagged as a stale path, and the bare word "yarn" in a
sentence about migrating away from it are all in `tests/test_drift.py`.

## Style

Explain *why* in comments, not *what*. The code says what it does; the comment
should say what would go wrong without it.
