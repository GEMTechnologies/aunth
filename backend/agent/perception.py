"""Assembling perception from several channels, with provenance at the centre.

THE RULE THIS IS BUILT AROUND
-----------------------------
§2C: "Do not interpret uncertain visual content as a verified organisational fact."

Granada fills funder forms with an organisation's registration number, budget and legal name. Those
values must come from verified records. A vision model that *reads* a number off a scanned certificate
has produced an observation, not a fact - and the difference is the whole safety property of this
module. So every perceived value carries where it came from and how reliable that channel is, and the
distinction is machine-checkable rather than a matter of developer discipline.

THE CHANNELS, AND WHY THEY ARE KEPT APART
-----------------------------------------
§2 requires vision and structure be used TOGETHER, not as substitutes. They fail in opposite ways:

  * STRUCTURE does not lie about what it declares - a field's `required` flag is either set or not -
    but it omits what is only rendered: a red border, a canvas, a banner drawn as an image.
  * VISION sees what the tree omits, but can misread, and cannot be asked to justify itself.

Neither is promoted above the other here. They are recorded as separate observations of the same page
and reconciled by the planner, which can see which channel said what.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from .multimodal_routing import ImageRef, Modality, Observation, required_modalities


class Channel(str, Enum):
    """Where an observation came from. Ordered by how much it can be trusted for a FACT."""

    #: A verified Granada record - a registration number from the organisation's own vault. The only
    #: channel that may supply a value used in a form.
    VERIFIED_RECORD = "VERIFIED_RECORD"
    #: Extracted deterministically from a document by a parser. Reliable for text, and reproducible.
    DOCUMENT_EXTRACTION = "DOCUMENT_EXTRACTION"
    #: The DOM or accessibility tree.
    STRUCTURE = "STRUCTURE"
    #: A vision-language model's reading of an image.
    VISION = "VISION"
    #: Text visible on an untrusted page. Never a fact, and never an instruction.
    PAGE_TEXT = "PAGE_TEXT"


#: Channels whose output may be written into a form as an organisational fact.
#: Deliberately a short allowlist: adding VISION here would be the single change that lets a
#: misread number become a statutory declaration.
FACT_CHANNELS: frozenset[Channel] = frozenset(
    {Channel.VERIFIED_RECORD, Channel.DOCUMENT_EXTRACTION}
)

#: Channels that are evidence but must be corroborated before they are treated as a fact.
CORROBORATION_CHANNELS: frozenset[Channel] = frozenset({Channel.STRUCTURE, Channel.VISION})


@dataclass(frozen=True)
class PerceivedValue:
    """One thing observed, and where it came from."""

    name: str
    value: Optional[str]
    channel: Channel
    #: A reference to the evidence - a document id, a field path, a screenshot ref. Never the content.
    evidence_ref: str = ""
    #: Only meaningful for VISION and DOCUMENT_EXTRACTION. A diagnostic signal, NOT proof: §12 says
    #: "treat model confidence as a diagnostic signal, not proof of correctness".
    confidence: Optional[float] = None
    observed_at: Optional[datetime] = None

    @property
    def is_fact(self) -> bool:
        """Whether this may be used as an organisational fact without corroboration."""
        return self.channel in FACT_CHANNELS and self.value not in (None, "")

    @property
    def needs_corroboration(self) -> bool:
        return self.channel in CORROBORATION_CHANNELS


@dataclass
class DocumentObservation:
    """What was understood about one document, and by what method.

    `method` is recorded because the two methods are not equivalent: a text layer is reproducible and
    quotable, while a vision reading of a scan is an interpretation. A consumer that cannot tell them
    apart cannot decide whether to trust the value.
    """

    document_id: str
    filename: str
    mime: str
    method: str                      # "text_extraction" | "vision" | "unsupported"
    text: str = ""
    #: Per-field readings, so a single extracted value can be traced to its page or region.
    values: list[PerceivedValue] = field(default_factory=list)
    page_count: int = 0
    #: True when this document required visual interpretation, which is the case §2C calls out.
    needed_vision: bool = False
    #: Why extraction did not produce a usable result, when it did not.
    limitation: Optional[str] = None

    @property
    def trustworthy_for_facts(self) -> bool:
        return self.method == "text_extraction" and not self.needed_vision and not self.limitation


@dataclass
class Perception:
    """Everything perceived about one page and its attachments.

    This is the input to planning. It is deliberately not a flat dictionary: separating structure from
    vision from records is what lets the planner answer "does Granada KNOW this, or does it only see
    something that looks like it?"
    """

    observation: Observation
    #: name -> every reading of it, from every channel. Multiple entries for one name is normal and is
    #: the signal that the page and the records disagree.
    values: dict[str, list[PerceivedValue]] = field(default_factory=dict)
    documents: list[DocumentObservation] = field(default_factory=list)
    #: Screenshots beyond the primary one, e.g. a cropped region.
    extra_images: list[ImageRef] = field(default_factory=list)
    #: Browser-level facts §2B asks for.
    tab_count: int = 1
    navigation_events: list[str] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    uploads: list[str] = field(default_factory=list)
    assembled_at: Optional[datetime] = None

    # -- what the planner needs ----------------------------------------------
    def fact(self, name: str) -> Optional[str]:
        """The value Granada may use as fact, or None.

        None is the honest answer when only a vision reading exists. Returning the best-looking guess
        instead is precisely what §2C forbids.
        """
        for v in self.values.get(name, []):
            if v.is_fact:
                return v.value
        return None

    def observed(self, name: str) -> Optional[str]:
        """The best available reading, INCLUDING unverified ones - for display and reconciliation,
        never for filling a form."""
        for v in self.values.get(name, []):
            if v.value:
                return v.value
        return None

    def conflicting(self) -> dict[str, list[PerceivedValue]]:
        """Names where channels disagree.

        §5 requires the engine to "identify contradictions between earlier answers and new form
        requirements". Two different values for one name is that contradiction, surfaced rather than
        resolved by picking one.
        """
        out: dict[str, list[PerceivedValue]] = {}
        for name, readings in self.values.items():
            distinct = {v.value for v in readings if v.value}
            if len(distinct) > 1:
                out[name] = list(readings)
        return out

    def unknown(self, required: list[str]) -> list[str]:
        """Which required names Granada cannot supply as fact.

        The directive: "If records are incomplete, it should identify precisely what is unknown." This
        is that list - and it is what a human gate is asked to fill, not something to guess at.
        """
        return [n for n in required if self.fact(n) in (None, "")]

    def needs_vision(self) -> bool:
        """Whether resolving this page requires a model that can see.

        Structural silence plus a picture means vision. Structural silence WITHOUT a picture means
        nobody can answer, which is a different problem - it is a gap, not a routing decision.
        """
        return self.observation.needs_vision

    def modalities(self, *, document: bool = False) -> frozenset[Modality]:
        return required_modalities(
            observation=self.observation,
            document_ref="doc" if (document or self.documents) else None,
        )


def assemble(
    *,
    observation: Observation,
    values: Optional[list[PerceivedValue]] = None,
    documents: Optional[list[DocumentObservation]] = None,
    extra_images: Optional[list[ImageRef]] = None,
    tab_count: int = 1,
    navigation_events: Optional[list[str]] = None,
    downloads: Optional[list[str]] = None,
    uploads: Optional[list[str]] = None,
    now: Optional[datetime] = None,
) -> Perception:
    """Combine the channels into one perception.

    Does NOT decide anything. It records what was seen and by which channel, so the decision - and the
    refusal - can happen later with the provenance visible.
    """
    grouped: dict[str, list[PerceivedValue]] = {}
    for v in values or []:
        grouped.setdefault(v.name, []).append(v)

    # Document readings are folded in too. Without this, a value understood from a document was
    # recorded on the DocumentObservation and then invisible to observed() - so a scanned certificate
    # read by a model produced evidence nobody could see. Found by scenario 8.
    for doc in documents or []:
        for v in doc.values:
            grouped.setdefault(v.name, []).append(v)

    return Perception(
        observation=observation,
        values=grouped,
        documents=list(documents or []),
        extra_images=list(extra_images or []),
        tab_count=tab_count,
        navigation_events=list(navigation_events or []),
        downloads=list(downloads or []),
        uploads=list(uploads or []),
        assembled_at=now or datetime.now(timezone.utc),
    )


def document_from_text(
    *,
    document_id: str,
    filename: str,
    mime: str,
    text: str,
    page_count: int = 0,
) -> DocumentObservation:
    """A document read deterministically, with no model involved.

    §2C: "Use appropriate document extraction tools first and vision-language models when visual
    interpretation is necessary." This is the first path, and it is the one that produces facts.
    """
    return DocumentObservation(
        document_id=document_id,
        filename=filename,
        mime=mime,
        method="text_extraction" if text else "unsupported",
        text=text,
        page_count=page_count,
        needed_vision=False,
        limitation=None if text else "no text layer was available",
    )


def document_from_vision(
    *,
    document_id: str,
    filename: str,
    mime: str,
    readings: list[PerceivedValue],
    screenshot_ref: str,
    limitation: Optional[str] = None,
) -> DocumentObservation:
    """A document read by looking at it - a scan, a photographed certificate, a chart.

    EVERY value it produces is recorded on the VISION channel, so `Perception.fact` will not return
    it. A scanned registration certificate read by a model is evidence that a certificate exists; it
    is not a verified registration number, and the type system here does not pretend otherwise.
    """
    tagged = [
        PerceivedValue(
            name=r.name,
            value=r.value,
            channel=Channel.VISION,
            evidence_ref=r.evidence_ref or screenshot_ref,
            confidence=r.confidence,
            observed_at=r.observed_at,
        )
        for r in readings
    ]
    return DocumentObservation(
        document_id=document_id,
        filename=filename,
        mime=mime,
        method="vision",
        values=tagged,
        needed_vision=True,
        limitation=limitation,
    )


def describe() -> dict[str, Any]:
    """The provenance rules, stated where a reviewer will find them."""
    return {
        "fact_channels": sorted(c.value for c in FACT_CHANNELS),
        "corroboration_channels": sorted(c.value for c in CORROBORATION_CHANNELS),
        "rule": (
            "only VERIFIED_RECORD and DOCUMENT_EXTRACTION may supply a value used as an "
            "organisational fact; a vision reading of a scan is evidence, not a fact"
        ),
        "confidence": "a diagnostic signal, never proof of correctness",
        "contradictions": (
            "surfaced by Perception.conflicting() rather than resolved by picking one value"
        ),
        "unknowns": "Perception.unknown() names precisely what is missing, for a human gate",
        "does_not_do": [
            "it does not decide actions - browser_runtime plans",
            "it does not route models - multimodal_routing does",
            "it does not store document bytes or image content",
        ],
    }
