"""Granada's confidence policy and rollout stages.

Two things are kept strictly apart here, because conflating them is how an
autonomous system ends up doing something it was never authorised to do:

**Confidence** is what the decision provider reported. It is evidence about one
answer.

**Authority** is whether Granada's own policy permits an action at all. It is a
statement about the organisation's settings, the decision type, and whether the
action has an external side effect.

A provider answering with confidence 1.0 authorises nothing. Confidence selects
which *workflow* an answer is allowed to feed; authority decides whether the
workflow may touch the outside world. ``PolicyEngine`` evaluates the second and
can refuse the first.

The bands below are **initial defaults, not calibrated thresholds**
--------------------------------------------------------------------
The brief says so explicitly and it matters: these numbers are a reasonable
starting guess, and treating them as calibration would be the same mistake as
trusting a vendor's benchmark. Before autonomy is enabled they must be replaced
with values measured against a labelled Granada dataset - which is what shadow
mode exists to produce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


class ConfidenceBand:
    VERY_HIGH = "VERY_HIGH"   # eligible for automated downstream action, IF authority permits
    HIGH = "HIGH"             # low-risk internal workflows
    MEDIUM = "MEDIUM"         # needs secondary verification or human review
    LOW = "LOW"               # no autonomous external side effect


#: (lower bound inclusive, band). Ordered highest first so lookup is a scan.
CONFIDENCE_BANDS: tuple[tuple[float, str], ...] = (
    (0.95, ConfidenceBand.VERY_HIGH),
    (0.85, ConfidenceBand.HIGH),
    (0.70, ConfidenceBand.MEDIUM),
    (0.00, ConfidenceBand.LOW),
)


def band_for(confidence: Optional[float]) -> str:
    """The band for a confidence value.

    ``None`` is LOW, emphatically. A provider that returns no confidence has not
    told Granada it is confident, and defaulting an absent number to a high band
    is how "we don't know" becomes "we're sure".
    """
    if confidence is None:
        return ConfidenceBand.LOW
    for threshold, band in CONFIDENCE_BANDS:
        if confidence >= threshold:
            return band
    return ConfidenceBand.LOW


class Autonomy:
    """The organisation's configured authority level, from the build brief."""

    MONITOR_ONLY = "MONITOR_ONLY"
    DRAFT_ONLY = "DRAFT_ONLY"
    AUTO_ROUTINE = "AUTO_ROUTINE"
    AUTOPILOT_WITH_GATES = "AUTOPILOT_WITH_GATES"

    #: Ordered least to most permissive, so a required level can be compared.
    ORDER = (MONITOR_ONLY, DRAFT_ONLY, AUTO_ROUTINE, AUTOPILOT_WITH_GATES)

    @classmethod
    def at_least(cls, configured: str, required: str) -> bool:
        try:
            return cls.ORDER.index(configured) >= cls.ORDER.index(required)
        except ValueError:
            # An unrecognised level is treated as the least permissive. A typo in
            # an autonomy setting must not grant authority.
            return False


class RolloutStage:
    """How much influence a decision provider is allowed to have.

    The brief is explicit that these are reached in order and never skipped. The
    default is SHADOW, which by construction influences nothing.
    """

    SHADOW = "SHADOW"
    ADVISORY = "ADVISORY"
    INTERNAL_AUTOMATION = "INTERNAL_AUTOMATION"
    LOW_RISK_EXTERNAL_AUTOMATION = "LOW_RISK_EXTERNAL_AUTOMATION"

    ORDER = (SHADOW, ADVISORY, INTERNAL_AUTOMATION, LOW_RISK_EXTERNAL_AUTOMATION)

    #: Stages at which a decision may actually drive something.
    ACTING = frozenset({INTERNAL_AUTOMATION, LOW_RISK_EXTERNAL_AUTOMATION})

    #: Actions that always require a human, at every stage and every autonomy
    #: level. Straight from the security gate: contract acceptance, bank and
    #: payment changes, legally binding declarations, material budget
    #: commitments, representation warranties, destructive actions, and anything
    #: whose terms forbid automated submission.
    ALWAYS_HUMAN = frozenset(
        {
            "accept_contract",
            "change_bank_details",
            "change_payment_details",
            "sign_legally_binding_declaration",
            "commit_material_budget",
            "make_representation_warranty",
            "destructive_action",
            "submit_where_automation_prohibited",
        }
    )


@dataclass
class PolicyOutcome:
    """Granada's verdict on whether an answer may drive anything."""

    allowed: bool
    #: ``acted`` is False in shadow and advisory stages even when ``allowed`` is
    #: True, and the distinction is kept: "policy permits this" and "we are
    #: currently letting it happen" are different facts.
    acted: bool
    reason: str
    band: str
    stage: str
    autonomy: str
    requires_human: bool = False
    #: True when the decision was recorded but deliberately had no effect.
    shadowed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "acted": self.acted,
            "reason": self.reason,
            "band": self.band,
            "stage": self.stage,
            "autonomy": self.autonomy,
            "requires_human": self.requires_human,
            "shadowed": self.shadowed,
        }


@dataclass
class DecisionPolicy:
    """Per-decision-type thresholds and required authority.

    Deliberately not a single global threshold. A 0.80 confidence is ample for
    "is this email an acknowledgement" and nowhere near enough for "is this
    organisation legally eligible", and one constant cannot express that.
    """

    decision_type: str
    #: Minimum confidence for the answer to feed anything at all.
    minimum_confidence: float = 0.70
    #: Minimum confidence for an internal workflow action.
    minimum_for_internal_action: float = 0.85
    #: Minimum confidence for any external side effect. Deliberately the highest
    #: bar in the structure.
    minimum_for_external_action: float = 0.95
    #: The authority level the organisation must have configured.
    required_autonomy: str = Autonomy.DRAFT_ONLY
    #: Internal actions can never leave the building.
    has_external_side_effect: bool = False
    #: Named actions that always route to a human regardless of confidence.
    always_human_actions: frozenset[str] = frozenset()
    #: Extra guard: any of these context flags forces a human.
    human_if_context: tuple[str, ...] = ()


#: Initial defaults per decision type. These encode the brief's judgement about
#: what is consequential, not a measurement. Shadow-mode evaluation exists to
#: replace them with measurements.
DEFAULT_POLICIES: dict[str, DecisionPolicy] = {
    "opportunity_triage": DecisionPolicy(
        decision_type="opportunity_triage",
        minimum_confidence=0.70,
        required_autonomy=Autonomy.MONITOR_ONLY,
        has_external_side_effect=False,
    ),
    "email_triage": DecisionPolicy(
        decision_type="email_triage",
        minimum_confidence=0.80,
        minimum_for_internal_action=0.90,
        # An auto-reply leaves the building, so the external bar applies.
        minimum_for_external_action=0.95,
        required_autonomy=Autonomy.AUTO_ROUTINE,
        has_external_side_effect=True,
        human_if_context=("contains_financial_request", "contains_legal_commitment"),
    ),
    "application_readiness": DecisionPolicy(
        decision_type="application_readiness",
        minimum_confidence=0.85,
        # Submission is the most consequential thing the platform does.
        minimum_for_external_action=0.98,
        required_autonomy=Autonomy.AUTOPILOT_WITH_GATES,
        has_external_side_effect=True,
        always_human_actions=frozenset({"submit_where_automation_prohibited"}),
    ),
    "agent_routing": DecisionPolicy(
        decision_type="agent_routing",
        minimum_confidence=0.75,
        required_autonomy=Autonomy.MONITOR_ONLY,
        has_external_side_effect=False,
        # Routing to a human is always allowed, however unsure the provider is.
        human_if_context=(),
    ),
    "follow_up": DecisionPolicy(
        decision_type="follow_up",
        minimum_confidence=0.85,
        minimum_for_external_action=0.95,
        required_autonomy=Autonomy.AUTO_ROUTINE,
        has_external_side_effect=True,
    ),
}


def policy_for(decision_type: str, overrides: Optional[dict[str, DecisionPolicy]] = None) -> DecisionPolicy:
    """Look up the policy, defaulting to the most restrictive one.

    An unknown decision type gets the external-side-effect posture. Defaulting to
    the permissive end would mean a new decision type is unbounded until someone
    remembers to configure it, which is the wrong direction for an error.
    """
    table = overrides or DEFAULT_POLICIES
    if decision_type in table:
        return table[decision_type]
    return DecisionPolicy(
        decision_type=decision_type,
        minimum_confidence=0.95,
        minimum_for_internal_action=0.95,
        minimum_for_external_action=0.99,
        required_autonomy=Autonomy.AUTOPILOT_WITH_GATES,
        has_external_side_effect=True,
    )
