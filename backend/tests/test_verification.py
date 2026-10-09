"""Verification. The tests that matter are the ones where the agent's own claim is REFUSED.

§11 asks for independence, and independence cannot come from a separate process - it is the same
server and the same code. It comes from the verifier not being allowed to cite the actor, and from the
expectation being registered before the action so events can contradict it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.verification import (  # noqa: E402
    ADMISSIBLE,
    Evidence,
    EvidenceSource,
    Expectation,
    Layer,
    VerificationResult,
    describe,
    may_claim_submitted,
    register_expectation,
    verify_execution,
    verify_outcome,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def exec_exp(**over):
    base = dict(kind="field_populated", detail="organisation_name holds the verified value", layer=Layer.EXECUTION)
    base.update(over)
    return register_expectation(**base)  # type: ignore[arg-type]


def outcome_exp(**over):
    base = dict(kind="submission_receipt", detail="a funder reference is issued", layer=Layer.OUTCOME, expect_value="OPP-1")
    base.update(over)
    return register_expectation(**base)  # type: ignore[arg-type]


# ===========================================================================
# THE ACTOR CANNOT VERIFY ITSELF
# ===========================================================================
def test_the_agents_own_claim_is_REFUSED_as_evidence():
    """THE test. If the verifier is fed the actor's report and asked whether it agrees, it agrees -
    and the check is theatre."""
    r = verify_execution(
        exec_exp(expect_value="Fictional NGO"),
        [Evidence(source=EvidenceSource.AGENT_CLAIM, detail="Fictional NGO", ref="agent-log")],
    )
    assert r.verified is False
    assert r.refused, "the inadmissible evidence should be recorded, not silently dropped"
    assert "own claim" in r.refused[0]


def test_the_agent_claim_is_never_admissible():
    assert EvidenceSource.AGENT_CLAIM not in ADMISSIBLE


def test_a_model_inference_alone_does_not_verify():
    """A model reading is a signal, not a fact - the same rule as perception's fact channels."""
    r = verify_execution(
        exec_exp(expect_value="Fictional NGO"),
        [Evidence(source=EvidenceSource.MODEL_INFERENCE, detail="looks like Fictional NGO")],
    )
    assert r.verified is False
    assert EvidenceSource.MODEL_INFERENCE not in ADMISSIBLE


def test_a_page_observation_DOES_verify_execution():
    r = verify_execution(
        exec_exp(expect_value="Fictional NGO"),
        [Evidence(source=EvidenceSource.PAGE_OBSERVATION, detail="field organisation_name = Fictional NGO")],
    )
    assert r.verified is True
    assert r.matched


def test_admissible_sources_are_an_explicit_allowlist():
    assert ADMISSIBLE == frozenset(
        {
            EvidenceSource.PAGE_OBSERVATION,
            EvidenceSource.EXTERNAL_RECEIPT,
            EvidenceSource.ARTEFACT,
            EvidenceSource.HUMAN_ATTESTATION,
        }
    )


# ===========================================================================
# THE EXPECTATION IS REGISTERED BEFORE THE ACTION
# ===========================================================================
def test_an_expectation_records_when_it_was_registered():
    """A prediction with a timestamp can be compared against observations made later, which is the
    only way an observation can contradict it."""
    e = register_expectation(kind="page_changed", detail="stage 2 shown", layer=Layer.EXECUTION, now=NOW)
    assert e.registered_at == NOW


def test_an_observation_that_contradicts_the_expectation_is_a_MISMATCH():
    r = verify_execution(
        exec_exp(expect_contains="Stage 2 of 3"),
        [Evidence(source=EvidenceSource.PAGE_OBSERVATION, detail="Stage 1 of 3")],
    )
    assert r.verified is False
    assert r.mismatches
    assert "Stage 2 of 3" in r.mismatches[0]


def test_no_evidence_is_inconclusive_not_a_pass():
    r = verify_execution(exec_exp(expect_value="x"), [])
    assert r.verified is False
    assert r.inconclusive is True


# ===========================================================================
# EXECUTION VERIFICATION CANNOT CLAIM AN OUTCOME
# ===========================================================================
def test_execution_verification_says_so_in_its_note():
    r = verify_execution(
        exec_exp(expect_value="Fictional NGO"),
        [Evidence(source=EvidenceSource.PAGE_OBSERVATION, detail="Fictional NGO")],
    )
    assert "does NOT establish any effect outside Granada" in r.note


def test_an_outcome_expectation_cannot_be_checked_as_execution():
    with pytest.raises(ValueError):
        verify_execution(outcome_exp(), [])


def test_a_page_change_expectation_cannot_be_checked_as_outcome():
    with pytest.raises(ValueError):
        verify_outcome(exec_exp(), [])


# ===========================================================================
# OUTCOME VERIFICATION REQUIRES AN EXTERNAL RECEIPT
# ===========================================================================
def test_a_submission_is_NOT_verified_by_a_confirmation_page():
    """§11: "Never mark an application SUBMITTED without authenticated submission evidence." A
    confirmation page Granada rendered itself is not a receipt."""
    r = verify_outcome(
        outcome_exp(),
        [Evidence(source=EvidenceSource.PAGE_OBSERVATION, detail="Thank you, your application was received")],
    )
    assert r.verified is False
    assert r.mismatches
    assert "not a receipt" in r.mismatches[0]


def test_a_populated_form_does_not_verify_a_submission():
    r = verify_outcome(
        outcome_exp(),
        [Evidence(source=EvidenceSource.ARTEFACT, detail="budget.pdf attached")],
    )
    assert r.verified is False


def test_an_external_receipt_naming_the_opportunity_DOES_verify():
    r = verify_outcome(
        outcome_exp(),
        [Evidence(source=EvidenceSource.EXTERNAL_RECEIPT, detail="receipt for OPP-1", ref="RCPT-9")],
    )
    assert r.verified is True
    assert r.matched


def test_a_receipt_for_the_WRONG_opportunity_is_a_mismatch():
    """A receipt proves something was submitted; it does not prove THIS was."""
    r = verify_outcome(
        outcome_exp(),
        [Evidence(source=EvidenceSource.EXTERNAL_RECEIPT, detail="receipt for OPP-999")],
    )
    assert r.verified is False
    assert "does not identify this opportunity" in r.mismatches[0]


def test_no_receipt_is_an_unknown_outcome_not_a_failure():
    """Which is what makes reconciliation possible instead of a retry.

    Two distinct cases, and the code distinguishes them:
      - evidence was offered and none of it was a receipt -> unknown, and it says so
      - no evidence was offered at all -> nothing to conclude, and it says THAT instead
    Conflating them would hide the difference between "we looked and could not tell" and "nobody
    looked", which is what an operator needs in order to know what to do next.
    """
    offered = verify_outcome(
        outcome_exp(),
        [Evidence(source=EvidenceSource.PAGE_OBSERVATION, detail="Thank you, your application was received")],
    )
    assert offered.verified is False
    assert "unknown rather than failed" in offered.note

    none = verify_outcome(outcome_exp(), [])
    assert none.verified is False
    assert "no evidence was offered" in none.note
    assert none.inconclusive is True


# ===========================================================================
# MAY WE CLAIM SUBMITTED?
# ===========================================================================
def test_submitted_requires_an_outcome_result_with_a_receipt():
    execution = verify_execution(
        exec_exp(expect_value="NGO"),
        [Evidence(source=EvidenceSource.PAGE_OBSERVATION, detail="NGO")],
    )
    assert may_claim_submitted([execution]) is False, "an execution pass must never imply a submission"


def test_a_verified_receipt_permits_the_claim():
    outcome = verify_outcome(
        outcome_exp(),
        [Evidence(source=EvidenceSource.EXTERNAL_RECEIPT, detail="receipt for OPP-1")],
    )
    assert may_claim_submitted([outcome]) is True


def test_both_layers_together_still_require_the_receipt():
    """Passing execution does not contribute to the outcome claim, by construction - only OUTCOME
    results are considered."""
    execution = verify_execution(
        exec_exp(expect_value="NGO"),
        [Evidence(source=EvidenceSource.PAGE_OBSERVATION, detail="NGO")],
    )
    page_outcome = verify_outcome(
        outcome_exp(),
        [Evidence(source=EvidenceSource.PAGE_OBSERVATION, detail="received")],
    )
    assert may_claim_submitted([execution, page_outcome]) is False


def test_the_agents_claim_cannot_produce_a_submission_even_for_the_outcome_layer():
    r = verify_outcome(
        outcome_exp(),
        [Evidence(source=EvidenceSource.AGENT_CLAIM, detail="I submitted it, reference OPP-1")],
    )
    assert r.verified is False
    assert may_claim_submitted([r]) is False


# ===========================================================================
# THE BOUNDARY IS STATED
# ===========================================================================
def test_describe_states_the_independence_mechanism():
    d = describe()
    assert "cannot cite the actor" in d["independence"]
    assert "AGENT_CLAIM" in d["inadmissible"]
    assert "never an effect outside Granada" in d["execution_verifies"]
    assert "EXTERNAL_RECEIPT" in d["outcome_requires"]
    assert "UNKNOWN outcome, not a failure" in d["uncertain_outcome"]
