"""Vision readings: the record, and the fact that anything asks for one.

THE GAP THIS GUARDS. `perception.read_page_with_vision` was fully written, unit-tested with a scripted
provider, and **called by nothing** - `BrowserRuntime` accepts a `vision_reader` and no production code
supplied one, so no screenshot had ever been read by a model. The platform had no OCR at all.

These tests pin the two things that would let it silently return to that state:
  * a reading is recorded with its text, and a failure is recorded rather than raised;
  * the browser handler actually asks for readings.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent import evidence_vision  # noqa: E402


def test_a_reading_formats_fields_values_and_obstacles():
    reading = evidence_vision.ScreenshotReading(
        path="/tmp/shot-1.png",
        ok=True,
        values=[
            {"name": "Email::present", "value": None},
            {"name": "Email", "value": "ngo@example.org"},
            {"name": "Amount::required_marker", "value": "true"},
            {"name": "obstacle", "value": "a modal covers the form"},
        ],
    )
    lines = reading.text_lines()
    assert "[field] Email" in lines
    assert "Email: ngo@example.org" in lines
    assert "[required] Amount" in lines
    assert "obstacle: a modal covers the form" in lines


def test_the_summary_counts_failures_and_keeps_the_text():
    readings = [
        evidence_vision.ScreenshotReading(path="/e/shot-1.png", ok=True,
                                          values=[{"name": "Title", "value": "Registration"}]),
        evidence_vision.ScreenshotReading(path="/e/shot-2.png", ok=False, error="file not found"),
    ]
    record = evidence_vision.summary(readings)

    assert record["screenshots_read"] == 1
    assert record["screenshots_failed"] == 1
    assert record["readings"][0]["file"] == "shot-1.png"
    assert "Title: Registration" in record["readings"][0]["text"]
    # A failure is named, not silently dropped: an absent reading must not look like a clean one.
    assert record["errors"][0]["file"] == "shot-2.png"
    assert record["errors"][0]["error"] == "file not found"


def test_the_browser_handler_asks_for_a_reading():
    """Reachability, not behaviour - the behaviour needs a model and is exercised live.

    Without this assertion the platform can go back to capturing screenshots that nothing ever reads,
    which is the state it was in for its entire history.
    """
    source = (BACKEND / "agent" / "workflow_engine.py").read_text(encoding="utf-8")
    assert "from agent.evidence_vision import read_screenshots" in source
    assert "read_screenshots(" in source
    # And the result must reach the record, or the reading happens and is thrown away.
    assert '"vision": vision_record' in source
