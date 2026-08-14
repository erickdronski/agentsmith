"""Findings and the evidence behind them.

The premise of this tool is that an AGENTS.md should describe what a repository
*actually does*, not what someone believed it did when they wrote the file. That
only works if every rule can be traced back to something countable.

So a detector never emits a bare instruction. It emits a :class:`Finding`, which
carries the rule, the evidence that produced it, and a confidence derived from
how dominant the pattern is. A rule backed by "83% of 412 test files" is a rule
a reader can argue with. A rule backed by nothing is the thing this tool exists
to replace.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "Evidence",
    "Finding",
    "Confidence",
    "dominant",
    "MIN_SAMPLE",
    "STRONG_SHARE",
    "WEAK_SHARE",
]

#: Below this many observations, a majority means very little. Ten files
#: agreeing is a convention; two files agreeing is a coincidence.
MIN_SAMPLE = 8

#: Share of observations that has to follow a pattern before it is stated as a
#: rule rather than as a tendency.
STRONG_SHARE = 0.85

#: Below this, there is no convention worth reporting — just variation.
WEAK_SHARE = 0.6


class Confidence:
    CERTAIN = "certain"     # Read directly from config. Not a sample.
    STRONG = "strong"       # Overwhelming majority of a real sample.
    LIKELY = "likely"       # Clear majority, meaningful exceptions.
    WEAK = "weak"           # A tendency. Reported, not asserted.

    ORDER = (CERTAIN, STRONG, LIKELY, WEAK)

    @staticmethod
    def rank(value: str) -> int:
        try:
            return Confidence.ORDER.index(value)
        except ValueError:
            return len(Confidence.ORDER)


class Evidence:
    """Where a finding came from, in a form a reader can go check.

    ``source`` names the artifact — a config file, ``git log``, a file glob.
    ``detail`` is the human sentence. ``samples`` are real paths, capped, so a
    skeptical reader can open one and see for themselves.
    """

    __slots__ = ("source", "detail", "observed", "total", "samples")

    def __init__(
        self,
        source: str,
        detail: str,
        observed: Optional[int] = None,
        total: Optional[int] = None,
        samples: Optional[Sequence[str]] = None,
    ) -> None:
        self.source = source
        self.detail = detail
        self.observed = observed
        self.total = total
        self.samples = list(samples or [])[:5]

    @property
    def share(self) -> Optional[float]:
        if self.observed is None or not self.total:
            return None
        return self.observed / self.total

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"source": self.source, "detail": self.detail}
        if self.observed is not None:
            payload["observed"] = self.observed
        if self.total is not None:
            payload["total"] = self.total
        if self.share is not None:
            payload["share"] = self.share
        if self.samples:
            payload["samples"] = self.samples
        return payload

    def summary(self) -> str:
        if self.share is not None:
            return "%s — %d of %d (%.0f%%)" % (
                self.detail,
                self.observed,
                self.total,
                self.share * 100,
            )
        return self.detail


class Finding:
    """One rule destined for the generated AGENTS.md."""

    __slots__ = ("key", "section", "rule", "confidence", "evidence", "note")

    def __init__(
        self,
        key: str,
        section: str,
        rule: str,
        confidence: str,
        evidence: Sequence[Evidence],
        note: Optional[str] = None,
    ) -> None:
        self.key = key
        self.section = section
        self.rule = rule
        self.confidence = confidence
        self.evidence = list(evidence)
        self.note = note

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "key": self.key,
            "section": self.section,
            "rule": self.rule,
            "confidence": self.confidence,
            "evidence": [item.to_dict() for item in self.evidence],
        }
        if self.note:
            payload["note"] = self.note
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Finding(%r, %r)" % (self.key, self.confidence)


def dominant(
    counts: Dict[str, int], min_sample: int = MIN_SAMPLE
) -> Optional[Dict[str, Any]]:
    """Find the winning value in a tally, with a confidence that reflects it.

    Returns ``None`` when the sample is too small to mean anything or when no
    option clears :data:`WEAK_SHARE`. Returning ``None`` matters more than it
    looks: a repo with genuinely mixed conventions should produce *no rule*
    rather than a rule asserting whichever style happens to lead by four files.
    Inventing consensus is exactly how a stale AGENTS.md gets written in the
    first place.
    """
    total = sum(counts.values())
    if total < min_sample:
        return None

    winner, observed = max(counts.items(), key=lambda item: (item[1], item[0]))
    share = observed / total
    if share < WEAK_SHARE:
        return None

    if share >= STRONG_SHARE:
        confidence = Confidence.STRONG
    elif share >= 0.7:
        confidence = Confidence.LIKELY
    else:
        confidence = Confidence.WEAK

    return {
        "value": winner,
        "observed": observed,
        "total": total,
        "share": share,
        "confidence": confidence,
        "runners_up": sorted(
            ((k, v) for k, v in counts.items() if k != winner),
            key=lambda item: -item[1],
        )[:3],
    }


def sort_findings(findings: Sequence[Finding]) -> List[Finding]:
    """Order for presentation: strongest evidence first within each section."""
    return sorted(
        findings,
        key=lambda f: (f.section, Confidence.rank(f.confidence), f.key),
    )
