"""Verification: did it happen, and did the thing that matters happen?

THE QUESTION THIS MODULE HAS TO ANSWER HONESTLY
-----------------------------------------------
§11 asks for an "independent verification pass" whose verifier "should consult observable evidence
rather than simply agreeing with the execution model."

But independence cannot come from running a different process - it is the same server, the same
database, the same code. If the verifier is fed the actor's own report and asked whether it agrees,
it will agree, and the check is theatre.

Independence here comes from three things that are structural rather than aspirational:

  1. THE EXPECTATION IS REGISTERED BEFORE THE ACTION. `Expectation` is built from the grounded
     action's `expected_result` and captured before anything is attempted. A verifier that is told
     afterwards what should have happened can always be satisfied; one holding a prediction made
     earlier can be contradicted by events.

  2. THE VERIFIER MAY NOT CITE THE ACTOR. `Evidence` carries a source, and evidence whose source is
     the acting agent's own claim is not admissible for verification. The verifier reads the page,
     the receipt, the uploaded file - not the log line saying the click succeeded.

  3. DISAGREEMENT IS A FIRST-CLASS RESULT. `VerificationResult.mismatches` is populated whenever the
     observation differs from the expectation, and `verified` is False. There is no path that turns
     "the agent believed it worked" into a pass.

THE TWO LAYERS, WHICH ANSWER DIFFERENT QUESTIONS
------------------------------------------------
Execution verification: did the browser do the thing? A field holds the value, a file is attached, the
next page loaded, the validation message disappeared.

Outcome verification: did the real-world result occur? §11 is explicit that these are not the same,
and that "never mark an application SUBMITTED without authenticated submission evidence" is the
standard. A populated form proves an action; only a receipt proves a submission.

§4 step 6 is the reason this exists at all: "Do not treat a successful mouse click or browser command
as proof of task completion."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional


class EvidenceSource(str, Enum):
    """Who produced a piece of evidence. The distinction the whole module rests on."""

    #: A fresh observation of the page or receipt. Admissible.
    PAGE_OBSERVATION = "PAGE_OBSERVATION"
    #: A receipt issued by the receiving system. The strongest evidence there is.
    EXTERNAL_RECEIPT = "EXTERNAL_RECEIPT"
    #: A file's own metadata - size, checksum, presence on disk. Admissible.
    ARTEFACT = "ARTEFACT"
    #: A named human. Admissible for facts only a human can attest.
    HUMAN_ATTESTATION = "HUMAN_ATTESTATION"
    #: THE ACTING AGENT'S OWN CLAIM. NOT admissible for verification.
    #:
    #: This member exists so the inadmissible case is a visible value rather than an oversight - a
    #: piece of evidence can be recorded as coming from the actor and then refused, which is more
    #: honest than silently dropping it.
    AGENT_CLAIM = "AGENT_CLAIM"
    #: A model's reading or interpretation. Admissible as a signal, never as a fact.
    MODEL_INFERENCE = "MODEL_INFERENCE"


#: What a verifier is allowed to rely on. AGENT_CLAIM is deliberately absent: an actor confirming its
#: own action is not verification, it is repetition.
ADMISSIBLE: frozenset[EvidenceSource] = frozenset(
    {
        EvidenceSource.PAGE_OBSERVATION,
        EvidenceSource.EXTERNAL_RECEIPT,
        EvidenceSource.ARTEFACT,
        EvidenceSource.HUMAN_ATTESTATION,
    }
)


@dataclass(frozen=True)
class Evidence:
    """One observable fact, and where it came from."""

    source: EvidenceSource
    detail: str
    ref: str = ""
    observed_at: Optional[datetime] = None

    @property
    def admissible(self) -> bool:
        return self.source in ADMISSIBLE


class Layer(str, Enum):
    EXECUTION = "EXECUTION"
    OUTCOME = "OUTCOME"


@dataclass(frozen=True)
class Expectation:
    """A prediction registered BEFORE the action, so events can contradict it.

    Built from `GroundedAction.expected_result`. `kind` names what would settle it, which is what
    makes the check concrete rather than a matter of opinion.
    """

    kind: str
    detail: str
    layer: Layer
    #: For a page-change expectation, the URL or marker that should now be present.
    expect_contains: Optional[str] = None
    #: For a field expectation, the value that should now be held.
    expect_value: Optional[str] = None
    registered_at: Optional[datetime] = None

    @property
    def is_outcome(self) -> bool:
        return self.layer is Layer.OUTCOME


@dataclass
class VerificationResult:
    """The verdict, with the evidence that produced it.

    `verified` False is NOT the same as failure - it means the evidence did not confirm, which is what
    makes an uncertain submission possible rather than an assumed one.
    """

    layer: Layer
    verified: bool
    expectation: Expectation
    matched: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    #: Evidence that was offered and refused, so a reader can see what was discarded and why.
    refused: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def inconclusive(self) -> bool:
        return not self.verified and not self.mismatches


def register_expectation(
    *,
    kind: str,
    detail: str,
    layer: Layer,
    expect_contains: Optional[str] = None,
    expect_value: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Expectation:
    """Record what should happen, before it is attempted.

    The timestamp is part of it: an expectation registered at a known moment can be compared against
    evidence observed *later*, which is the only way an observation can contradict a prediction.
    """
    return Expectation(
        kind=kind,
        detail=detail,
        layer=layer,
        expect_contains=expect_contains,
        expect_value=expect_value,
        registered_at=now or datetime.now(timezone.utc),
    )


def _split(evidence: list[Evidence]) -> tuple[list[Evidence], list[str]]:
    admissible: list[Evidence] = []
    refused: list[str] = []
    for e in evidence:
        if e.admissible:
            admissible.append(e)
        else:
            refused.append(
                f"{e.source.value}: {e.detail!r} is the acting agent's own claim and cannot verify "
                "the action it describes"
            )
    return admissible, refused


def verify_execution(expectation: Expectation, evidence: list[Evidence]) -> VerificationResult:
    """Did the browser do the thing?

    Deliberately narrow: this layer can only ever establish that an ACTION occurred. Passing it is not
    evidence that anything happened outside Granada, and nothing here may be used to conclude that an
    application was submitted.
    """
    if expectation.layer is not Layer.EXECUTION:
        raise ValueError("verify_execution requires an EXECUTION expectation")
    if expectation.is_outcome:
        raise ValueError("an outcome expectation cannot be checked as execution")

    admissible, refused = _split(evidence)
    matched: list[str] = []
    mismatches: list[str] = []

    for e in admissible:
        text = f"{e.detail} {e.ref}".lower()
        if expectation.expect_value is not None:
            if expectation.expect_value.lower() in text:
                matched.append(e.detail)
            else:
                mismatches.append(
                    f"expected the value {expectation.expect_value!r} to be present; the observation "
                    f"reports {e.detail!r}"
                )
        elif expectation.expect_contains is not None:
            if expectation.expect_contains.lower() in text:
                matched.append(e.detail)
            else:
                mismatches.append(
                    f"expected {expectation.expect_contains!r}; the observation reports {e.detail!r}"
                )
        else:
            matched.append(e.detail)

    verified = bool(matched) and not mismatches
    return VerificationResult(
        layer=Layer.EXECUTION,
        verified=verified,
        expectation=expectation,
        matched=matched,
        mismatches=mismatches,
        refused=refused,
        note=(
            "the action was observed to occur; this does NOT establish any effect outside Granada"
            if verified
            else "the observation did not confirm the action"
        ),
    )


def verify_outcome(expectation: Expectation, evidence: list[Evidence]) -> VerificationResult:
    """Did the real-world result occur?

    §11: "Never mark an application SUBMITTED without authenticated submission evidence."

    Only an EXTERNAL_RECEIPT satisfies a submission expectation. A populated form, a confirmation page
    Granada rendered itself, or a model's reading are all insufficient - each is recorded as a
    mismatch rather than quietly counted.
    """
    if expectation.layer is not Layer.OUTCOME:
        raise ValueError("verify_outcome requires an OUTCOME expectation")

    admissible, refused = _split(evidence)
    matched: list[str] = []
    mismatches: list[str] = []

    if expectation.kind == "submission_receipt":
        receipts = [e for e in admissible if e.source is EvidenceSource.EXTERNAL_RECEIPT]
        if not receipts:
            others = [e for e in admissible if e.source is not EvidenceSource.EXTERNAL_RECEIPT]
            for e in others:
                mismatches.append(
                    f"{e.source.value} evidence ({e.detail!r}) is not a receipt and cannot establish "
                    "that an application was submitted"
                )
            return VerificationResult(
                layer=Layer.OUTCOME,
                verified=False,
                expectation=expectation,
                mismatches=mismatches,
                refused=refused,
                note=(
                    "no authenticated submission evidence; the outcome is unknown rather than failed"
                    if mismatches
                    else "no evidence was offered"
                ),
            )
        for e in receipts:
            if expectation.expect_value and expectation.expect_value.lower() not in f"{e.detail} {e.ref}".lower():
                mismatches.append(
                    f"a receipt was issued but does not identify this opportunity: {e.detail!r}"
                )
            else:
                matched.append(e.detail)
    else:
        for e in admissible:
            text = f"{e.detail} {e.ref}".lower()
            if expectation.expect_value is not None:
                (matched if expectation.expect_value.lower() in text else mismatches).append(
                    e.detail if expectation.expect_value.lower() in text
                    else f"expected {expectation.expect_value!r}; observed {e.detail!r}"
                )
            elif expectation.expect_contains is not None:
                (matched if expectation.expect_contains.lower() in text else mismatches).append(
                    e.detail if expectation.expect_contains.lower() in text
                    else f"expected {expectation.expect_contains!r}; observed {e.detail!r}"
                )
            else:
                matched.append(e.detail)

    return VerificationResult(
        layer=Layer.OUTCOME,
        verified=bool(matched) and not mismatches,
        expectation=expectation,
        matched=matched,
        mismatches=mismatches,
        refused=refused,
        note=(
            "the outcome was established from external evidence"
            if matched and not mismatches
            else "the outcome was not established"
        ),
    )


def may_claim_submitted(results: list[VerificationResult]) -> bool:
    """Whether an application may be recorded as SUBMITTED.

    Requires an OUTCOME-layer result that verified against an external receipt. Execution results are
    ignored entirely - they cannot contribute, by construction, because only OUTCOME results are
    considered.
    """
    return any(
        r.verified and r.layer is Layer.OUTCOME and r.expectation.kind == "submission_receipt"
        for r in results
    )


def describe() -> dict[str, Any]:
    """The rules, stated where a reviewer will find them."""
    return {
        "admissible_evidence": sorted(s.value for s in ADMISSIBLE),
        "inadmissible": [EvidenceSource.AGENT_CLAIM.value, EvidenceSource.MODEL_INFERENCE.value],
        "independence": (
            "the verifier cannot cite the actor: evidence from AGENT_CLAIM is refused, and the "
            "expectation is registered before the action so events can contradict it"
        ),
        "execution_verifies": "that an action occurred - never an effect outside Granada",
        "outcome_requires": "an EXTERNAL_RECEIPT for a submission; a confirmation page is not one",
        "uncertain_outcome": (
            "no receipt is an UNKNOWN outcome, not a failure - which is what makes reconciliation "
            "possible instead of a retry"
        ),
        "does_not_do": [
            "it does not perform actions - browser_runtime does",
            "it does not reconcile - submission_lifecycle does",
        ],
    }
