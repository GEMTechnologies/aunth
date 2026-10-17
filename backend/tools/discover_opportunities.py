"""Run a funding discovery sweep.

WHAT THIS IS FOR. It is the executable form of the funding search bot: it fetches live opportunities
from the configured sources, ingests them into the shared catalogue, and prints what it did. Running
it is also how an operator proves a source still works after it changes its schema, without waiting
for the schedule.

    python tools/discover_opportunities.py --limit 20
    python tools/discover_opportunities.py --source grants_gov --keywords "humanitarian,climate" --json

EXIT CODE IS MEANINGFUL. Zero only when the sweep both reached its sources and stored what it
fetched. A non-zero exit is what lets this be put on a schedule and alerted on, instead of an
operator having to read the JSON to notice that a source has been returning nothing for a week.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys


def _split(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and ingest funding opportunities.")
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        help="Source to sweep (repeatable). Defaults to the configured set.",
    )
    parser.add_argument(
        "--keywords",
        default="",
        help="Comma-separated search terms. Defaults to the NGO-relevant set.",
    )
    parser.add_argument("--limit", type=int, default=25, help="Maximum records per source.")
    parser.add_argument(
        "--no-detail",
        action="store_true",
        help="Skip the per-opportunity detail call (faster, no amounts or eligibility).",
    )
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    parser.add_argument("--verbose", action="store_true", help="Include debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    from agent.opportunity_discovery import DEFAULT_KEYWORDS, DEFAULT_SOURCES, run_and_commit
    from database import SessionLocal

    source_names = tuple(args.source) if args.source else DEFAULT_SOURCES
    keywords = tuple(_split(args.keywords)) if args.keywords else DEFAULT_KEYWORDS

    db = SessionLocal()
    try:
        summary = run_and_commit(
            db,
            source_names=source_names,
            keywords=keywords,
            limit=args.limit,
            with_detail=not args.no_detail,
        )
    except Exception as exc:  # noqa: BLE001 - the tool reports, it does not traceback at an operator
        print(f"discovery failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()

    if args.json:
        print(json.dumps(summary.as_dict(), indent=2))
    else:
        print(f"sources swept : {len(summary.fetches)}")
        print(f"found         : {summary.found}")
        print(f"stored        : {summary.stored}")
        counts = summary.counts
        print(
            "  created={created} updated={updated} unchanged={unchanged} "
            "duplicate={duplicate} rejected={rejected}".format(
                created=counts["CREATED"],
                updated=counts["UPDATED"],
                unchanged=counts["UNCHANGED"],
                duplicate=counts["DUPLICATE_DELIVERY"],
                rejected=counts["REJECTED"],
            )
        )
        for error in summary.errors[:10]:
            print(f"  source error: {error}")

    # A sweep that reached nothing, or reached its sources but stored nothing, is a failure worth
    # alerting on - the quiet version of this is a bot that has been dead for a week.
    if not summary.ok:
        return 1
    if summary.found and not summary.stored:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
