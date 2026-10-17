"""Apply the browser evidence retention policy.

DRY RUN BY DEFAULT, and that is not timidity. This is the one command in the system that destroys
material a customer's audit trail may reference. An operator should read the plan, decide, and then
ask for it a second time before anything is unlinked.

    python tools/prune_evidence.py                       # show the plan, change nothing
    python tools/prune_evidence.py --apply               # actually delete
    python tools/prune_evidence.py --full-days 14 --sampled-days 60 --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# RUNNING AS A SCRIPT PUTS `tools/` ON sys.path, not the backend root, so `agent` does not resolve.
# The container sets PYTHONPATH=/app/backend and hides this; a host invocation does not, and the
# failure is a bare ModuleNotFoundError that says nothing about the fix. Pinned here so the command
# works the same way in both places.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from agent.evidence_retention import (  # noqa: E402
    DEFAULT_KEEP_FULL_DAYS,
    DEFAULT_KEEP_SAMPLED_DAYS,
    DEFAULT_SAMPLE_FRAMES,
    RetentionPolicy,
    apply_prune,
    plan_prune,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or apply evidence retention.")
    parser.add_argument("--root", default=None, help="Evidence root. Defaults to the configured one.")
    parser.add_argument("--full-days", type=int, default=DEFAULT_KEEP_FULL_DAYS)
    parser.add_argument("--sampled-days", type=int, default=DEFAULT_KEEP_SAMPLED_DAYS)
    parser.add_argument("--sample-frames", type=int, default=DEFAULT_SAMPLE_FRAMES)
    parser.add_argument("--max-bytes", type=int, default=None, help="Hard cap across the whole store.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this the command only reports the plan.",
    )
    parser.add_argument("--json", action="store_true", help="Print the plan as JSON.")
    args = parser.parse_args(argv)

    try:
        policy = RetentionPolicy(
            keep_full_days=args.full_days,
            keep_sampled_days=args.sampled_days,
            sample_frames=args.sample_frames,
            max_total_bytes=args.max_bytes,
        )
    except Exception as exc:  # noqa: BLE001 - a bad policy is a usage error, not a traceback
        print(f"invalid policy: {exc}")
        return 2

    plan = plan_prune(args.root, policy)

    if args.json:
        print(json.dumps(plan.as_dict(), indent=2))
    else:
        print(f"runs scanned      : {plan.runs_scanned}")
        print(f"runs downsampled  : {plan.runs_downsampled}")
        print(f"runs released     : {plan.runs_released}")
        print(f"files to keep     : {len(plan.keep)}")
        print(f"files to delete   : {len(plan.delete)}")
        print(f"bytes freed       : {plan.bytes_freed:,}")

    if not args.apply:
        if plan.delete:
            print("\nDRY RUN - nothing was deleted. Re-run with --apply to remove the files above.")
        return 0

    result = apply_prune(plan, root=args.root)
    print(f"deleted           : {result.deleted}")
    print(f"bytes freed       : {result.bytes_freed:,}")
    for failure in result.failed[:20]:
        print(f"  refused: {failure}")
    # A refusal means the plan and the disk disagreed, or the plan was unsafe. Not success.
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
