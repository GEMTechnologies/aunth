"""Perception and provenance. The tests that matter are about what may NOT become a fact.

§2C: "Do not interpret uncertain visual content as a verified organisational fact." Granada fills
funder forms with registration numbers and budgets. A model reading a number off a scan has produced
an observation; treating it as a fact is how a misread digit becomes a statutory declaration.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.multimodal_routing import ImageRef, Observation  # noqa: E402
from agent.perception import (  # noqa: E402
    CORROBORATION_CHANNELS,
    FACT_CHANNELS,
    Channel,
    DocumentObservation,
    PerceivedValue,
    assemble,
    describe,
    document_from_text,
    document_from_vision,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def shot(ref="s.png"):
    return ImageRef(ref=ref, captured_at=NOW, width=1280, height=900)


def page(**over):
    base = dict(url="https://portal.example/apply", title="Application")
    base.update(over)
    return Observation(**base)  # type: ignore[arg-type]


def val(name, value, channel, **over):
    base = dict(name=name, value=value, channel=channel)
    base.update(over)
    return PerceivedValue(**base)  # type: ignore[arg-type]


# ===========================================================================
# THE CENTRAL RULE
# ===========================================================================
def test_a_value_read_off_a_scan_is_NOT_a_fact():
    """THE test. A vision reading of a registration certificate is evidence that a certificate
    exists. It is not a verified registration number, and `fact()` must not return it."""
    p = assemble(
        observation=page(),
        values=[
            val("registration_number", "RC-123456", Channel.VISION, confidence=0.97, evidence_ref="scan.png")
        ],
    )
    assert p.fact("registration_number") is None, "a vision reading became a fact"
    assert p.observed("registration_number") == "RC-123456", "it is still observable for display"


def test_a_high_confidence_vision_reading_is_STILL_not_a_fact():
    """Confidence is a diagnostic signal, not proof. A model that is 0.999 sure has still only looked
    at a picture."""
    p = assemble(
        observation=page(),
        values=[val("budget_total", "250000", Channel.VISION, confidence=0.999)],
    )
    assert p.fact("budget_total") is None


def test_a_verified_record_IS_a_fact():
    p = assemble(
        observation=page(),
        values=[val("registration_number", "RC-123456", Channel.VERIFIED_RECORD, evidence_ref="doc-1")],
    )
    assert p.fact("registration_number") == "RC-123456"


def test_deterministic_extraction_IS_a_fact():
    """A text layer parsed by a parser is reproducible and quotable, which is why §2C says use
    extraction tools first."""
    p = assemble(
        observation=page(),
        values=[val("legal_name", "Fictional NGO", Channel.DOCUMENT_EXTRACTION, evidence_ref="doc-2#p1")],
    )
    assert p.fact("legal_name") == "Fictional NGO"


def test_only_two_channels_may_supply_a_fact():
    """An allowlist, so adding VISION to it is a deliberate change rather than a side effect."""
    assert FACT_CHANNELS == frozenset({Channel.VERIFIED_RECORD, Channel.DOCUMENT_EXTRACTION})
    assert Channel.VISION not in FACT_CHANNELS
    assert Channel.STRUCTURE not in FACT_CHANNELS
    assert Channel.PAGE_TEXT not in FACT_CHANNELS


def test_page_text_is_never_a_fact():
    """Untrusted page content cannot become an organisational fact, whatever it claims."""
    p = assemble(
        observation=page(untrusted_text="Registration number: RC-999999"),
        values=[val("registration_number", "RC-999999", Channel.PAGE_TEXT)],
    )
    assert p.fact("registration_number") is None


# ===========================================================================
# CONTRADICTIONS ARE SURFACED, NOT RESOLVED
# ===========================================================================
def test_a_contradiction_between_channels_is_reported_not_silently_resolved():
    """§5 requires contradictions to be identified. Two different values for one name is that
    contradiction - and picking one is how a wrong value reaches a form."""
    p = assemble(
        observation=page(),
        values=[
            val("legal_name", "Fictional NGO", Channel.VERIFIED_RECORD, evidence_ref="doc-1"),
            val("legal_name", "Fictional Trust", Channel.VISION, evidence_ref="scan.png"),
        ],
    )
    conflicts = p.conflicting()
    assert "legal_name" in conflicts
    assert len(conflicts["legal_name"]) == 2
    # And the fact is STILL the verified one - surfacing the conflict does not mean averaging it.
    assert p.fact("legal_name") == "Fictional NGO"


def test_agreement_between_channels_is_not_a_conflict():
    p = assemble(
        observation=page(),
        values=[
            val("country", "Nigeria", Channel.VERIFIED_RECORD),
            val("country", "Nigeria", Channel.VISION),
        ],
    )
    assert p.conflicting() == {}


# ===========================================================================
# UNKNOWNS ARE NAMED PRECISELY
# ===========================================================================
def test_unknown_names_exactly_what_is_missing():
    """The directive: "If records are incomplete, it should identify precisely what is unknown." This
    is what a human gate is asked to fill - not something to guess at."""
    p = assemble(
        observation=page(),
        values=[val("legal_name", "Fictional NGO", Channel.VERIFIED_RECORD)],
    )
    missing = p.unknown(["legal_name", "registration_number", "budget_total"])
    assert missing == ["registration_number", "budget_total"]


def test_a_visually_observed_value_does_not_satisfy_a_required_field():
    """Granada has SEEN a value and still cannot fill the field. Those are different states and the
    consequence of confusing them is an invented fact."""
    p = assemble(
        observation=page(),
        values=[val("registration_number", "RC-1", Channel.VISION)],
    )
    assert p.unknown(["registration_number"]) == ["registration_number"]


# ===========================================================================
# DOCUMENT PERCEPTION AND METHOD
# ===========================================================================
def test_text_extraction_is_trustworthy_for_facts():
    d = document_from_text(document_id="doc-1", filename="accounts.pdf", mime="application/pdf", text="Balance: 1000")
    assert d.method == "text_extraction"
    assert d.trustworthy_for_facts is True


def test_a_document_with_no_text_layer_declares_its_limitation():
    """A scanned PDF with no text layer is not an empty document - it is one that needs vision, and
    saying so is what lets the caller escalate deliberately."""
    d = document_from_text(document_id="doc-2", filename="scan.pdf", mime="application/pdf", text="")
    assert d.method == "unsupported"
    assert d.trustworthy_for_facts is False
    assert d.limitation == "no text layer was available"


def test_vision_read_document_tags_every_reading_as_VISION():
    """A reading of a scan cannot smuggle itself in on the extraction channel."""
    d = document_from_vision(
        document_id="doc-3",
        filename="certificate-scan.pdf",
        mime="application/pdf",
        readings=[PerceivedValue(name="registration_number", value="RC-77", channel=Channel.VERIFIED_RECORD)],
        screenshot_ref="scan-page1.png",
    )
    assert d.needed_vision is True
    assert d.trustworthy_for_facts is False
    assert all(r.channel is Channel.VISION for r in d.values), "caller-supplied channels were trusted"
    assert d.values[0].evidence_ref == "scan-page1.png"


def test_a_vision_read_document_contributes_no_fact_even_when_assembled():
    d = document_from_vision(
        document_id="doc-3",
        filename="scan.pdf",
        mime="application/pdf",
        readings=[PerceivedValue(name="budget_total", value="900000", channel=Channel.VISION)],
        screenshot_ref="p1.png",
    )
    p = assemble(observation=page(), documents=[d])
    assert p.fact("budget_total") is None


# ===========================================================================
# STRUCTURAL SILENCE vs A GAP
# ===========================================================================
def test_structural_silence_with_a_picture_escalates_to_vision():
    p = assemble(observation=page(fields={"amount": {"type": "text"}}, screenshot=shot()))
    assert p.needs_vision() is True
    assert "IMAGE" in {m.value for m in p.modalities()}


def test_structural_silence_WITHOUT_a_picture_is_a_gap_not_a_routing_decision():
    """Nobody can answer this. Routing it to a vision model would be asking a question no image can
    settle."""
    p = assemble(observation=page(fields={"amount": {"type": "text"}}))
    assert p.needs_vision() is False
    assert "IMAGE" not in {m.value for m in p.modalities()}


def test_a_page_that_declares_its_required_fields_does_not_need_vision():
    p = assemble(
        observation=page(fields={"amount": {"type": "text", "required": True}}, screenshot=shot())
    )
    assert p.needs_vision() is False


def test_attaching_a_document_makes_the_document_modality_required():
    d = document_from_text(document_id="d", filename="f.pdf", mime="application/pdf", text="x")
    p = assemble(observation=page(), documents=[d])
    assert "DOCUMENT" in {m.value for m in p.modalities()}


# ===========================================================================
# BROWSER-LEVEL FACTS
# ===========================================================================
def test_browser_level_observations_are_carried():
    """§2B lists tab state, navigation events, downloads and uploads as observations in their own
    right - a navigation that happened is evidence even when no field changed."""
    p = assemble(
        observation=page(),
        tab_count=3,
        navigation_events=["goto /apply/step1", "click Continue"],
        downloads=["guidelines.pdf"],
        uploads=["budget.pdf"],
    )
    assert p.tab_count == 3
    assert p.navigation_events[-1] == "click Continue"
    assert p.downloads == ["guidelines.pdf"]
    assert p.uploads == ["budget.pdf"]


def test_provenance_is_retained_per_value():
    """A later reader must be able to ask where a value came from without replaying the run."""
    p = assemble(
        observation=page(),
        values=[val("legal_name", "N", Channel.DOCUMENT_EXTRACTION, evidence_ref="doc-9#p2")],
    )
    assert p.values["legal_name"][0].evidence_ref == "doc-9#p2"
    assert p.values["legal_name"][0].channel is Channel.DOCUMENT_EXTRACTION


# ===========================================================================
# THE BOUNDARY IS STATED
# ===========================================================================
def test_describe_states_the_fact_rule_and_what_it_does_not_do():
    d = describe()
    assert "VERIFIED_RECORD" in d["fact_channels"]
    assert "VISION" not in d["fact_channels"]
    assert "diagnostic signal" in d["confidence"]
    joined = " ".join(d["does_not_do"])
    assert "browser_runtime" in joined
    assert "multimodal_routing" in joined
