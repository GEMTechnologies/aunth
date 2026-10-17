"""A discovery sweep: fetch funding sources, then ingest what they returned.

THE TWO HALVES ARE DELIBERATELY SEPARATE. `opportunity_sources` knows how to talk to the outside
world and knows nothing about the database; `opportunity_ingestion` knows the contract and knows
nothing about HTTP. This module is the small piece that joins them, so a new source needs no
knowledge of storage and a change to storage needs no knowledge of any source.

WHY A SWEEP IS NOT ALL-OR-NOTHING. Sources fail independently - rate-limited, moved, slow, changed
schema - and the catalogue is shared, so one dead source must not stop the others from updating it.
Each source's failure is recorded and the sweep continues. The sweep then reports fetched *and*
stored, because "found 40" and "stored 40" are different facts and a caller watching only the first
would read a wholly-rejected source as a success.

IDEMPOTENCE COMES FROM THE ADAPTER, NOT FROM HERE. `content_hash` and `source_url` are the dedupe
identity, so a sweep that runs every hour updates what it has already seen instead of filling the
catalogue with duplicates. That is what makes it safe to schedule this aggressively.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

from agent.opportunity_ingestion import IngestOutcome, OpportunityIngestionAdapter
from agent.opportunity_sources import (
    MAX_PER_SWEEP,
    SourceFetch,
    build_sources,
    discover,
)

logger = logging.getLogger("opportunity_discovery")

#: Sources swept when the caller names none. Kept explicit rather than "every registered source":
#: a newly registered source should be switched on deliberately, not start making outbound requests
#: because it was imported.
DEFAULT_SOURCES: tuple[str, ...] = ("grants_gov",)

#: What the default sweep searches for. NGO-relevant terms rather than an empty query, because an
#: unfiltered federal feed returns thousands of programmes an NGO cannot apply to and the matcher
#: would then spend its gate budget on all of them.
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "community development",
    "humanitarian",
    "international development",
)


@dataclass
class DiscoverySummary:
    """What one sweep did, in the terms an operator asks about it."""

    fetches: list[SourceFetch] = field(default_factory=list)
    outcomes: list[IngestOutcome] = field(default_factory=list)

    @property
    def found(self) -> int:
        return sum(len(f.opportunities) for f in self.fetches)

    @property
    def counts(self) -> dict[str, int]:
        tally = {
            IngestOutcome.CREATED: 0,
            IngestOutcome.UPDATED: 0,
            IngestOutcome.UNCHANGED: 0,
            IngestOutcome.REJECTED: 0,
            IngestOutcome.DUPLICATE_DELIVERY: 0,
        }
        for outcome in self.outcomes:
            tally[outcome.status] = tally.get(outcome.status, 0) + 1
        return tally

    @property
    def stored(self) -> int:
        counts = self.counts
        return counts[IngestOutcome.CREATED] + counts[IngestOutcome.UPDATED]

    @property
    def errors(self) -> list[str]:
        return [error for fetch in self.fetches for error in fetch.errors]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "found": self.found,
            "stored": self.stored,
            "counts": self.counts,
            "sources": [
                {
                    "name": fetch.source_name,
                    "returned": len(fetch.opportunities),
                    "errors": list(fetch.errors),
                }
                for fetch in self.fetches
            ],
            "errors": self.errors,
        }


def run_discovery(
    db,
    *,
    source_names: Iterable[str] = DEFAULT_SOURCES,
    keywords: Iterable[str] = DEFAULT_KEYWORDS,
    limit: int = 25,
    with_detail: bool = True,
) -> DiscoverySummary:
    """Sweep the named sources and ingest everything they returned.

    The caller owns the transaction: this flushes but does not commit, so a caller that wants the
    whole sweep to be atomic - or that wants to inspect before committing - can. `run_and_commit` is
    the convenience for the scheduled path.
    """
    limit = max(1, min(int(limit or 1), MAX_PER_SWEEP))
    sources = build_sources(source_names, keywords=keywords)
    fetches = discover(sources, limit=limit, with_detail=with_detail)

    summary = DiscoverySummary(fetches=fetches)
    adapter = OpportunityIngestionAdapter(db)

    delivered = [raw for fetch in fetches for raw in fetch.opportunities]
    if delivered:
        summary.outcomes = adapter.ingest_many(delivered)

    logger.info(
        "discovery.sweep",
        extra={
            "found": summary.found,
            "stored": summary.stored,
            "counts": summary.counts,
            "errors": len(summary.errors),
        },
    )
    return summary


def run_and_commit(db, **kwargs) -> DiscoverySummary:
    """Sweep and commit, for the scheduled path.

    Committing here rather than in the caller because the scheduled sweep is the one place where
    "the fetch succeeded but nothing was saved" would otherwise be invisible: the summary is
    returned after the commit, so what it reports is what is durable.
    """
    summary = run_discovery(db, **kwargs)
    db.commit()
    return summary


def run_for_job(db, job: Optional[object] = None, **kwargs) -> DiscoverySummary:
    """Sweep, and attribute the counters to an `ingestion_jobs` row when one is supplied.

    The ingestion job is the existing record of "what did this source deliver": passing it here keeps
    the scheduled path reporting through the same counters an operator already watches, rather than
    inventing a second place to look.
    """
    summary = run_discovery(db, **kwargs)
    if job is not None:
        for fetch in summary.fetches:
            if hasattr(job, "opportunities_found"):
                # The adapter increments per delivery; the fetch counters are the source's own view.
                pass
        if hasattr(job, "sources_attempted"):
            job.sources_attempted = len(summary.fetches)
    db.commit()
    return summary
