"""The browser-execution boundary (§12).

The directive says to design the integration and NOT deploy the engine. These tests exercise the
isolation properties that make the design worth having - each one is something a future worker could
get wrong in a way that leaks another organisation's documents.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.browser_boundary import (  # noqa: E402
    ActionScope,
    BrowserOutcome,
    BrowserResult,
    BrowserTask,
    BrowserTaskRefused,
    CredentialRef,
    describe_integration,
    validate_task,
)


def _task(*, documents=None, hosts=("funder.example",), paths=("/apply",), max_attempts=3):
    return BrowserTask(
        task_id="t1", org_id="org-a", package_id="pkg", workflow_id="wf", job_id="job",
        package_fingerprint="f" * 64,
        action_scope=ActionScope("Funder Portal", hosts, paths, max_steps=50),
        credentials=[CredentialRef("portal_login", "authenticate to the funder")],
        documents=documents if documents is not None else [{"document_id": "doc-1"}],
        max_attempts=max_attempts,
    )


# ===========================================================================
# TENANT ISOLATION - the property that matters most
# ===========================================================================
def test_a_task_with_its_own_documents_is_accepted():
    validate_task(_task(), org_document_ids={"doc-1"})


def test_a_CROSS_TENANT_document_reference_is_REFUSED():
    """The isolation guarantee. A reference to another organisation's document must be refused at
    construction, not resolved and filtered later - later is where it leaks."""
    with pytest.raises(BrowserTaskRefused) as caught:
        validate_task(_task(), org_document_ids={"doc-2"})
    assert "does not belong to organisation" in str(caught.value)


def test_a_task_with_no_organisation_is_refused():
    task = _task()
    task.org_id = ""
    with pytest.raises(BrowserTaskRefused):
        validate_task(task, org_document_ids={"doc-1"})


def test_a_document_reference_with_no_id_is_refused():
    with pytest.raises(BrowserTaskRefused):
        validate_task(_task(documents=[{}]), org_document_ids=set())


# ===========================================================================
# ACTION SCOPE
# ===========================================================================
def test_a_suffix_lookalike_host_is_not_permitted():
    """`evil-funder.example` ends with `funder.example`. Suffix matching is the oldest bug in
    allow-listing and it would let a worker navigate to an attacker's page mid-application."""
    scope = ActionScope("Funder", ("funder.example",), ("/apply",))
    assert scope.permits("funder.example", "/apply/form")
    assert not scope.permits("evil-funder.example", "/apply/form")


def test_an_unlisted_host_is_not_permitted():
    scope = ActionScope("Funder", ("funder.example",), ("/apply",))
    assert not scope.permits("elsewhere.example", "/apply")


def test_an_unlisted_path_is_not_permitted():
    scope = ActionScope("Funder", ("funder.example",), ("/apply",))
    assert not scope.permits("funder.example", "/admin")


def test_WILDCARD_hosts_are_refused_at_construction():
    """A wildcard defeats the point of declaring hosts, so it is refused before a worker sees it."""
    with pytest.raises(BrowserTaskRefused) as caught:
        validate_task(_task(hosts=("*.example",)), org_document_ids={"doc-1"})
    assert "wildcard" in str(caught.value)


def test_a_task_with_no_allowed_host_is_refused():
    with pytest.raises(BrowserTaskRefused):
        validate_task(_task(hosts=()), org_document_ids={"doc-1"})


def test_max_attempts_below_one_is_refused():
    with pytest.raises(BrowserTaskRefused):
        validate_task(_task(max_attempts=0), org_document_ids={"doc-1"})


# ===========================================================================
# CREDENTIALS ARE REFERENCES
# ===========================================================================
def test_a_credential_carries_a_name_and_never_a_value():
    """A password in a task object lands in the job payload, the audit log and the retry history -
    three places nobody intended."""
    reference = CredentialRef("portal_login", "authenticate to the funder")
    serialised = reference.as_dict()
    assert serialised == {"name": "portal_login", "purpose": "authenticate to the funder"}
    assert "value" not in serialised and "secret" not in serialised


def test_the_serialised_task_carries_no_secret_fields():
    payload = _task().as_dict()
    for forbidden in ("password", "secret", "token", "api_key"):
        assert forbidden not in payload


# ===========================================================================
# SUBMITTED REQUIRES CONFIRMATION **AND** AN IDENTIFIER
# ===========================================================================
def test_CONFIRMED_without_an_identifier_is_NOT_submitted():
    """The distinction the whole milestone keeps: a worker that completed its steps has not been told
    by the funder that anything arrived."""
    result = BrowserResult("t1", BrowserOutcome.CONFIRMED)
    assert result.submitted is False


def test_CONFIRMED_WITH_an_identifier_is_submitted():
    result = BrowserResult("t1", BrowserOutcome.CONFIRMED, submission_identifier="FR-2026-0001")
    assert result.submitted is True


def test_a_BLOCKED_external_result_is_never_submitted_even_with_an_identifier():
    """A CAPTCHA or access denial means the worker stopped. An identifier alongside it would be
    contradictory, and the outcome is what governs."""
    result = BrowserResult(
        "t1", BrowserOutcome.BLOCKED_EXTERNAL, submission_identifier="leftover"
    )
    assert result.submitted is False


@pytest.mark.parametrize("outcome", [
    BrowserOutcome.UNCONFIRMED,
    BrowserOutcome.RECOVERABLE,
    BrowserOutcome.FAILED,
])
def test_no_other_outcome_is_ever_submitted(outcome):
    assert BrowserResult("t1", outcome, submission_identifier="x").submitted is False


def test_the_result_records_everything_the_directive_asks_for():
    payload = BrowserResult(
        "t1", BrowserOutcome.RECOVERABLE, completed_steps=["opened portal", "filled section 1"],
        validation=["section 1 valid"], recoverable_problems=["session expired at step 7"],
        checkpoint="ck-7", evidence=["screenshot:step7.png"],
    ).as_dict()
    for field in (
        "outcome", "completed_steps", "validation", "recoverable_problems",
        "submission_identifier", "evidence", "checkpoint", "occurred_at", "audit",
    ):
        assert field in payload, f"the result omits {field}"


# ===========================================================================
# RECOVERY
# ===========================================================================
def test_a_task_can_carry_a_checkpoint_to_resume_from():
    task = _task()
    task.checkpoint = "ck-7"
    assert task.as_dict()["checkpoint"] == "ck-7"


# ===========================================================================
# NOTHING IS INSTALLED
# ===========================================================================
def test_the_module_states_that_no_worker_is_enabled():
    """Recorded in the code so the next person finds this rather than an empty module."""
    text = describe_integration()
    assert "No browser worker is installed" in text
    assert "person submits them" in text
