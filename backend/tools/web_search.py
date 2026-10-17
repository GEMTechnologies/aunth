"""Ask the open web a question, from the command line.

    python tools/web_search.py --query "UN Partner Portal NGO registration requirements"
    python tools/web_search.py --fetch https://supportcso.unpartnerportal.org/...
    python tools/web_search.py --question "how does an NGO register on the UN Partner Portal" --json

WHY A CLI AND NOT ONLY A WORKFLOW STEP. The single most valuable thing this can do for this product is
answer the question the browser task has been trying to discover by driving the portal - which documents
and steps an NGO needs - for one search-plus-fetch instead of a multi-minute browser session. An operator
should be able to ask that in one command, and see the same pages the agent would cite.

`--question` searches and then reads the strongest results, which is the shape of a real research task.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Search and fetch the live web.")
    parser.add_argument("--query", help="search query (supports site:, after:, quoted phrases)")
    parser.add_argument("--fetch", metavar="URL", help="retrieve one page as Markdown")
    parser.add_argument("--question", help="search, then read the top results")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--max-chars", type=int, default=3000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from config import settings
    from agent.web_research import (
        WebResearchError,
        fetch,
        research_question,
        search,
    )

    if not (args.query or args.fetch or args.question):
        parser.print_help()
        return 2

    try:
        if args.fetch:
            page = fetch(args.fetch, max_chars=args.max_chars, settings=settings)
            if args.json:
                print(json.dumps(page.__dict__, indent=2))
            else:
                print(f"{page.title}\n{page.url}\n{'-' * 60}\n{page.content}")
            return 0

        if args.question:
            outcome = research_question(args.question, count=args.count, settings=settings)
            if args.json:
                print(json.dumps(outcome, indent=2))
            else:
                if not outcome["ok"]:
                    print(f"FAIL  {outcome['error']}")
                    return 1
                for hit in outcome["hits"]:
                    print(f"[{hit['score']:.2f}] {hit['title']}\n       {hit['url']}")
                for page in outcome["pages"]:
                    print(f"\n=== {page.get('title') or page['url']}\n{page.get('content', page.get('error', ''))[:1200]}")
            return 0 if outcome.get("ok") else 1

        hits = search(args.query, count=args.count, settings=settings)
        if args.json:
            print(json.dumps([hit.__dict__ for hit in hits], indent=2))
        elif not hits:
            print("no results")
        else:
            for hit in hits:
                print(f"[{hit.score:.2f}] {hit.title}")
                print(f"       {hit.url}")
                if hit.summary:
                    print(f"       {hit.summary[:220]}")
        return 0
    except WebResearchError as exc:
        print(f"FAIL  {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
