"""agentsmith — mine a repository's actual conventions into an AGENTS.md.

Deterministic, offline, and dependency-free. Every rule it emits was derived
from committed code, configuration, or git history, and carries the evidence
that produced it.

The premise: an instruction file written from memory is wrong the day after it
is written, and an agent following a stale one goes wrong confidently. So the
file should be derived from the repository, and drift between the two should be
detectable in CI rather than discovered by a confused agent.

    python3 -m agentsmith            # print an AGENTS.md for the current repo
    python3 -m agentsmith --explain  # with the evidence for every rule
    python3 -m agentsmith --check    # report drift in an existing file
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
