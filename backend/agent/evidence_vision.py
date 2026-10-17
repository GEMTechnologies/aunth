"""Read text out of the screenshots the browser captured.

WHY THIS EXISTS, AND WHY IT IS HERE RATHER THAN IN THE WORKER.

The platform had no OCR at all: no Tesseract, no EasyOCR, no PDF text extraction. Its only image pathway
was `perception.read_page_with_vision`, which was fully written, unit-tested with a scripted provider, and
**called by nothing** - `BrowserRuntime` accepts a `vision_reader` and no production code ever supplied
one. So no screenshot had ever been read by a model.

It cannot be wired inside the browser worker either, and that is deliberate: the worker is given neither a
DSN nor a model key, because it runs hostile page content. A callable cannot cross a process boundary, so
the reading has to happen where the credentials are - here, in the executor, over the evidence the run
left behind.

WHAT THIS GUARANTEES

* **Every value is VISION-channel.** `perception.FACT_CHANNELS` excludes VISION, so text read off a
  picture can never be written into a form as an organisation fact. A scanned certificate proves a
  certificate exists; it does not prove a registration number.
* **A failure is recorded, not raised.** One unreadable screenshot must not fail a run that has already
  happened - the screenshots are the record of what the browser did, and losing the record because the
  model was unavailable would be worse than having no reading.
* **No bytes are stored here.** Images are addressed by path and loaded on demand, matching
  `perception.describe()`'s promise that the perception layer does not hold image content.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

#: Bumped when the prompt or the schema changes, so a reading can be attributed to the prompt that made
#: it. Same reason `prompt_version` exists on every other model call.
PROMPT_VERSION = "evidence-vision/v1"


@dataclass
class ScreenshotReading:
    path: str
    ok: bool
    values: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    def text_lines(self) -> list[str]:
        """The readable text, in the order the model reported it."""
        out: list[str] = []
        for value in self.values:
            name = str(value.get("name") or "")
            raw = value.get("value")
            if name.endswith("::present"):
                out.append(f"[field] {name[: -len('::present')]}")
            elif name.endswith("::required_marker"):
                out.append(f"[required] {name[: -len('::required_marker')]}")
            elif raw is not None:
                out.append(f"{name}: {raw}")
        return out


def _load_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def read_screenshots(
    *,
    db: Any,
    org_id: str,
    paths: Iterable[str],
    settings: Any = None,
    tier: str = "CLASSIFICATION",
    limit: int = 12,
    #: GENEROUS ON PURPOSE. Reasoning tokens come out of the same allowance, and DeepSeek Flash spends
    #: a lot of them: measured at 1024 the call succeeded, the schema validated, and every reading was
    #: EMPTY - which reads as "the model cannot see" when in fact it never got to the answer.
    max_output_tokens: int = 4096,
) -> list[ScreenshotReading]:
    """Ask a vision model to read each screenshot and report what it can see.

    Bounded by `limit`: a run can leave dozens of captures and reading all of them is spending without
    thinking. The caller gets one reading per file it asked about, including failures.
    """
    from agent import perception
    from agent.model_gateway import build_gateway_from_settings
    from agent.multimodal_routing import ImageRef, Observation

    if settings is None:
        from config import settings as settings  # type: ignore[assignment]

    try:
        gateway = build_gateway_from_settings(db, settings)
    except Exception as exc:  # noqa: BLE001 - an unconfigured gateway is a state, not a crash
        logger.warning("evidence_vision.gateway_unavailable", extra={"error": type(exc).__name__})
        return [
            ScreenshotReading(path=str(p), ok=False, error=f"no model gateway: {type(exc).__name__}")
            for p in list(paths)[:limit]
        ]

    readings: list[ScreenshotReading] = []
    for raw_path in list(paths)[:limit]:
        path = str(raw_path)
        if not Path(path).is_file():
            # The reference surviving the run is the whole point of the evidence store; a missing file
            # is recorded rather than skipped, because a silently absent reading looks like a clean one.
            readings.append(ScreenshotReading(path=path, ok=False, error="file not found"))
            continue

        # A bare observation: no structural fields, no controls, one screenshot. That is exactly the
        # condition `needs_vision` describes - the DOM said nothing and there is a picture to look at.
        observation = Observation(
            url="",
            title="",
            screenshot=ImageRef(
                ref=path, captured_at=datetime.now(timezone.utc), width=0, height=0, label=Path(path).name
            ),
        )

        try:
            values = perception.read_page_with_vision(
                observation,
                gateway=gateway,
                org_id=org_id,
                prompt_version=PROMPT_VERSION,
                tier=tier,
                image_loader=_load_bytes,
                max_output_tokens=max_output_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - never fail a run for a reading
            logger.warning(
                "evidence_vision.read_failed", extra={"error": type(exc).__name__, "path": Path(path).name}
            )
            readings.append(ScreenshotReading(path=path, ok=False, error=f"{type(exc).__name__}: {exc}"[:200]))
            continue

        readings.append(
            ScreenshotReading(
                path=path,
                ok=True,
                values=[
                    {"name": v.name, "value": v.value, "channel": getattr(v.channel, "value", v.channel)}
                    for v in values
                ],
            )
        )

    return readings


def summary(readings: list[ScreenshotReading]) -> dict[str, Any]:
    """A compact record for the activity, with the text a person can read."""
    succeeded = [r for r in readings if r.ok]
    return {
        "prompt_version": PROMPT_VERSION,
        "screenshots_read": len(succeeded),
        "screenshots_failed": len(readings) - len(succeeded),
        "readings": [
            {"file": Path(r.path).name, "text": r.text_lines()[:40]} for r in succeeded
        ],
        "errors": [{"file": Path(r.path).name, "error": r.error} for r in readings if not r.ok][:5],
    }
