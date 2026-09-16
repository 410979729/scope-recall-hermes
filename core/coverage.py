"""Say what a bounded read did not look at, instead of silently not looking.

Every read is bounded -- by a SQL ``LIMIT``, by the request deadline, by the
number of packet slots -- and a recall that scanned eight of forty candidates
must not look like one that scanned all forty.  "The memory does not know"
and "the memory did not look" are different answers, and a system whose value
is refusing to guess has to be able to say *why* it refuses.

The vocabulary is one gap string per truncation, carrying the stage, how many
candidates were considered, and how many there were::

    coverage_truncated:profile_terms:8of8+
    coverage_truncated:background_deadline:5of17

A trailing ``+`` means "at least this many", which is what a ``LIMIT n+1``
probe can honestly prove.  An exact total would cost a second aggregate query
on every read to refine a number nobody acts on differently; knowing that
more exist is the part that changes behaviour.

Not responsible for: deciding the bounds, or what a host does with the finding.
"""
from __future__ import annotations

from typing import Any

#: Prefix shared by every truncation gap.  The packet compiler publishes
#: unrecognised prefixes intact, so the counts reach the host.
COVERAGE_GAP_PREFIX = "coverage_truncated"


def truncation_gap(stage: str, *, considered: int, available: int, at_least: bool = False) -> str | None:
    """One gap describing a truncation, or ``None`` when nothing was cut.

    ``at_least`` marks a total proved by over-fetching rather than counted.
    """
    if considered < 0 or available <= considered:
        return None
    return f"{COVERAGE_GAP_PREFIX}:{stage}:{considered}of{available}{'+' if at_least else ''}"


def note_truncation(gaps: list[str] | None, stage: str, *, considered: int, available: int,
                    at_least: bool = False) -> None:
    """Append a truncation gap when there is one.  Accepts ``None`` for callers
    that have no gap list, so a read path never has to branch around this."""
    if gaps is None:
        return
    gap = truncation_gap(stage, considered=considered, available=available, at_least=at_least)
    if gap is not None and gap not in gaps:
        gaps.append(gap)


def parse_coverage_gap(gap: Any) -> dict[str, Any] | None:
    """Read a truncation gap back into its parts, or ``None`` if it is not one."""
    if type(gap) is not str or not gap.startswith(COVERAGE_GAP_PREFIX + ":"):
        return None
    try:
        _prefix, stage, counts = gap.split(":", 2)
        at_least = counts.endswith("+")
        considered, available = (counts[:-1] if at_least else counts).split("of", 1)
        return {
            "stage": stage,
            "considered": int(considered),
            "available": int(available),
            "at_least": at_least,
        }
    except (ValueError, TypeError):
        return None


__all__ = ["COVERAGE_GAP_PREFIX", "note_truncation", "parse_coverage_gap", "truncation_gap"]
