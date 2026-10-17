"""Read text out of the screenshots the browser captured.

    python tools/read_evidence.py --package <package_id>
    python tools/read_evidence.py --package <package_id> --org <org_id> --json

WHAT THIS IS FOR. The platform had no OCR: no Tesseract, no EasyOCR, no PDF text extraction, and the one
image pathway (`perception.read_page_with_vision`) was called by nothing. This asks a vision model to read
the screenshots a browser run left behind and reports the text it could see.

The reading is EVIDENCE, not fact. Every value is VISION-channel, and `perception.FACT_CHANNELS` excludes
VISION - so text read off a picture can never be written into a form as an organisation's own claim. A
scanned certificate proves a certificate exists; it does not prove the number on it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

EVIDENCE_ROOT = Path("/var/lib/granada/browser-evidence")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Read text from captured screenshots.")
    parser.add_argument("--package", required=True, help="package id (the evidence directory name)")
    parser.add_argument("--org", default="3d4edeec-f4dc-4e5f-a99d-29100780b3f2", help="organisation id")
    parser.add_argument("--limit", type=int, default=4, help="how many screenshots to read")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    directory = EVIDENCE_ROOT / args.org / args.package
    if not directory.is_dir():
        print(f"FAIL  no evidence at {directory}")
        return 1

    paths = sorted(p for p in directory.glob("*.png") if p.is_file())
    if not paths:
        print(f"FAIL  no screenshots in {directory}")
        return 1

    from database import FleetSessionLocal, SessionLocal
    from tenant_context import set_tenant
    from agent.evidence_vision import read_screenshots, summary

    db = None
    for factory in (SessionLocal, FleetSessionLocal):
        try:
            db = factory()
            break
        except Exception:  # noqa: BLE001
            continue
    if db is None:
        print("FAIL  could not open a database session")
        return 1

    try:
        try:
            set_tenant(db, args.org, None)
        except Exception:  # noqa: BLE001 - binding is best-effort for a read-only tool
            pass
        readings = read_screenshots(
            db=db, org_id=args.org, paths=[str(p) for p in paths], limit=args.limit
        )
        record = summary(readings)
    finally:
        db.close()

    if args.json:
        print(json.dumps(record, indent=2))
    else:
        print(f"read {record['screenshots_read']}/{len(paths)} screenshots "
              f"({record['screenshots_failed']} failed)")
        for entry in record["readings"]:
            print(f"\n=== {entry['file']}")
            for line in entry["text"]:
                print(f"  {line}")
            if not entry["text"]:
                print("  (the model reported nothing it could read)")
        for problem in record["errors"]:
            print(f"! {problem['file']}: {problem['error']}")

    return 0 if record["screenshots_read"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
