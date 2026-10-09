"""Declared model capabilities, and a router that refuses rather than degrades.

THE FAILURE THIS PREVENTS
-------------------------
A model that cannot see receiving an image-dependent task does not raise. It answers anyway. Handed a
screenshot and asked "which field is highlighted in red?", a text-only model produces something
fluent and wrong, and nothing downstream can tell that no pixels were ever examined.

In a system that fills funder forms on an organisation's behalf, that is the worst possible failure
mode: a confident answer with no evidence behind it. So the rule here is not "prefer a vision model"
- it is that a task DECLARES what it needs, a model DECLARES what it has, and a mismatch is a refusal
rather than a degraded attempt.

WHAT THIS BUILDS ON
-------------------
`agent/model_gateway.py` already owns providers, routing, cost budgets, retries and durable
`ModelInvocation` records. None of that is duplicated here. This module adds the two things the
inspection found missing - a declared capability, and image input on a request - and nothing else.

THE STALENESS RULE
------------------
§7: "Do not click coordinates derived from a stale screenshot." A screenshot is an observation with a
timestamp, not a permanent truth about a page. `Observation.is_current` makes that checkable, and the
router carries the capture time through so a planner can refuse to act on a stale frame instead of
clicking wherever a field used to be.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

# ---------------------------------------------------------------------------
# What a task needs, and what a model has
# ---------------------------------------------------------------------------
class Modality(str, Enum):
    """An input a model may or may not be able to consume."""

    TEXT = "TEXT"
    IMAGE = "IMAGE"
    #: A PDF or DOCX handed to the model as a document, not as extracted text. Distinct from IMAGE
    #: because providers differ: some accept an image of a page, few accept a PDF, and a model that
    #: handles one has not thereby declared the other.
    DOCUMENT = "DOCUMENT"
    #: Deterministic computation - extraction with a parser, not inference. Satisfiable with no model
    #: at all, which is why it is a modality rather than an assumption.
    STRUCTURED = "STRUCTURED"


#: Never true by default. A capability must be POSITIVELY declared, so a new provider added by
#: someone else is assumed incapable until they say otherwise - the safe direction for a refusal.
DEFAULT_CAPABILITIES: frozenset[Modality] = frozenset({Modality.TEXT})


@dataclass(frozen=True)
class ModelProfile:
    """One configured model and what it can actually do.

    `provider` and `model` are names for routing and for the audit record; nothing here calls out.
    """

    name: str
    provider: str
    model: str
    capabilities: frozenset[Modality] = DEFAULT_CAPABILITIES
    #: Refused above this. A provider that rejects an oversized image usually does so with a generic
    #: error, and discovering the limit by failure wastes a call and obscures the cause.
    max_image_bytes: Optional[int] = None
    max_image_pixels: Optional[int] = None
    #: Relative cost, for choosing between two models that could both do the job. Not currency -
    #: the units are the operator's.
    cost_weight: float = 1.0
    #: Whether this model is approved to receive an organisation's documents. Kept per-model because
    #: a route that is cheap but not permitted is not a route.
    permitted_for_tenant_data: bool = False

    def can(self, *needed: Modality) -> bool:
        return all(m in self.capabilities for m in needed)


@dataclass(frozen=True)
class ImageRef:
    """A screenshot or cropped region, by reference and never by content.

    Bytes are deliberately absent. What travels through the planner is a reference, a size and a
    capture time - so a reasoning trace, a log or an error message cannot leak an organisation's
    document by carrying the image itself.
    """

    ref: str
    captured_at: datetime
    width: int
    height: int
    byte_size: int = 0
    mime: str = "image/png"
    #: What part of the page this is. A cropped region is grounded to the viewport it was cut from,
    #: which is what lets a coordinate be checked against the frame it came from.
    viewport_width: Optional[int] = None
    viewport_height: Optional[int] = None
    label: str = ""

    def is_current(self, *, now: Optional[datetime] = None, max_age: Optional[timedelta] = None) -> bool:
        """Whether this observation still describes the page.

        The bound is supplied per call because it is a property of the SITE, not of the image: a
        static confirmations page stays valid for minutes, a live upload progress bar does not.
        """
        if max_age is None:
            return True
        moment = now or datetime.now(timezone.utc)
        captured = self.captured_at
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        return (moment - captured) <= max_age


@dataclass(frozen=True)
class Observation:
    """Everything perceived about one page, from every channel.

    Combining them is the point: §2 requires that vision and structure be used TOGETHER. The
    structural fields give precise, actionable facts; the image covers what the DOM omits - a field
    marked required only by a red border, a warning banner rendered as a picture, a modal that
    overlaps the tree.

    `untrusted_text` is separated from every other field and is never a source of instruction.
    """

    url: str
    title: str = ""
    #: name -> {label, type, required, options}
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    controls: list[str] = field(default_factory=list)
    validation_messages: list[str] = field(default_factory=list)
    #: The accessibility tree, by reference.
    accessibility_ref: Optional[str] = None
    #: The DOM, by reference.
    dom_ref: Optional[str] = None
    screenshot: Optional[ImageRef] = None
    #: Page text. Data, never instruction.
    untrusted_text: str = ""

    @property
    def has_structure(self) -> bool:
        return bool(self.fields or self.controls or self.accessibility_ref)

    @property
    def needs_vision(self) -> bool:
        """Whether this observation can only be resolved by looking at it.

        True when the structure is empty or silent but a picture exists - the case where a page is
        rendered in a way the tree does not describe, such as a canvas, an image-only form, or a
        required marker expressed as a coloured border.
        """
        if self.screenshot is None:
            return False
        if not self.has_structure:
            return True
        return not any(f.get("required") for f in self.fields.values()) and not self.validation_messages


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
class NoCapableModel(RuntimeError):
    """No configured model can perform the task AS DESCRIBED.

    Raised rather than falling back to a weaker model, because the fallback is exactly the failure
    mode this module exists to prevent.
    """


@dataclass(frozen=True)
class RoutingDecision:
    profile: ModelProfile
    because: str
    evidence: dict[str, Any] = field(default_factory=dict)


def required_modalities(
    *,
    observation: Optional[Observation] = None,
    document_ref: Optional[str] = None,
    structured_only: bool = False,
) -> frozenset[Modality]:
    """What a task actually needs, derived from the task rather than declared by the caller.

    Deriving it means a caller cannot forget to declare that it is passing an image - which is how a
    text-only model ends up receiving one.
    """
    if structured_only:
        # A deterministic extraction. No inference is required, and spending a vision call on it is
        # exactly the waste §3 warns about.
        return frozenset({Modality.STRUCTURED})

    needed: set[Modality] = {Modality.TEXT}
    if observation is not None and observation.screenshot is not None:
        needed.add(Modality.IMAGE)
    if document_ref is not None:
        needed.add(Modality.DOCUMENT)
    return frozenset(needed)


def route(
    profiles: list[ModelProfile],
    *,
    needed: frozenset[Modality],
    requires_tenant_data: bool = False,
    prefer_cheapest: bool = True,
) -> RoutingDecision:
    """Choose a model, or refuse.

    Refusal is the point. A task needing IMAGE and a fleet of text-only models must produce
    `NoCapableModel`, not a text model's guess about a screenshot.
    """
    capable = [p for p in profiles if p.can(*needed)]
    if not capable:
        missing = sorted(m.value for m in needed)
        available = sorted({m.value for p in profiles for m in p.capabilities})
        raise NoCapableModel(
            f"no configured model can handle {missing}; available capabilities across "
            f"{len(profiles)} profile(s) are {available or ['none']}. Refusing rather than routing "
            "an image-dependent task to a model that cannot see it."
        )

    if requires_tenant_data:
        permitted = [p for p in capable if p.permitted_for_tenant_data]
        if not permitted:
            raise NoCapableModel(
                f"{len(capable)} model(s) can handle {sorted(m.value for m in needed)} but none is "
                "permitted to receive tenant data; refusing rather than sending an organisation's "
                "records to a route that is not approved for them"
            )
        capable = permitted

    # Offer the strongest first only when it matters; otherwise cheapest capable, because §3 asks
    # that trivial work not be escalated to an expensive multimodal model.
    chosen = min(capable, key=lambda p: p.cost_weight) if prefer_cheapest else max(
        capable, key=lambda p: len(p.capabilities)
    )
    return RoutingDecision(
        profile=chosen,
        because=(
            f"{chosen.name} is the cheapest of {len(capable)} capable profile(s) for "
            f"{sorted(m.value for m in needed)}"
            if prefer_cheapest
            else f"{chosen.name} is the most capable for {sorted(m.value for m in needed)}"
        ),
        evidence={
            "needed": sorted(m.value for m in needed),
            "capable": [p.name for p in capable],
            "requires_tenant_data": requires_tenant_data,
            "excluded_for_tenant_data": [
                p.name for p in profiles if p.can(*needed) and not p.permitted_for_tenant_data
            ] if requires_tenant_data else [],
        },
    )


def check_image(profile: ModelProfile, image: ImageRef) -> None:
    """Refuse an image the chosen model cannot accept.

    Checked BEFORE the call, because a provider's rejection message is usually generic and the real
    cause - a limit, or a model that never accepted images - is then guessed at.
    """
    if Modality.IMAGE not in profile.capabilities:
        raise NoCapableModel(
            f"{profile.name} does not declare IMAGE; refusing to send it {image.label or image.ref}"
        )
    if profile.max_image_bytes is not None and image.byte_size > profile.max_image_bytes:
        raise NoCapableModel(
            f"{profile.name} accepts at most {profile.max_image_bytes} bytes per image; "
            f"{image.ref} is {image.byte_size}"
        )
    if (
        profile.max_image_pixels is not None
        and image.width * image.height > profile.max_image_pixels
    ):
        raise NoCapableModel(
            f"{profile.name} accepts at most {profile.max_image_pixels} pixels; "
            f"{image.ref} is {image.width}x{image.height}"
        )


def escalation_order(profiles: list[ModelProfile]) -> list[ModelProfile]:
    """Cheapest first, so a caller escalates deliberately rather than starting at the top.

    §3: "Avoid expensive vision calls after every trivial click when deterministic verification is
    sufficient." Ordering by cost makes the cheap path the path of least resistance.
    """
    return sorted(profiles, key=lambda p: (p.cost_weight, p.name))


def describe() -> dict[str, Any]:
    """The rules, stated where a reviewer will find them."""
    return {
        "capabilities_are_declared_not_inferred": True,
        "default_capabilities": sorted(m.value for m in DEFAULT_CAPABILITIES),
        "refuses_on_mismatch": True,
        "refuses_on_tenant_data_mismatch": True,
        "builds_on": "agent.model_gateway (providers, cost budgets, ModelInvocation records)",
        "does_not_do": [
            "it does not call a provider - model_gateway does",
            "it does not store images - references only, never bytes",
            "it does not treat page text as instruction",
        ],
        "staleness": (
            "ImageRef.is_current takes the bound per call, because the sensible age is a property of "
            "the SITE: a static confirmation stays valid for minutes, a live upload bar does not"
        ),
    }
