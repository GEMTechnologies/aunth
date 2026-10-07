"""The application workspace state machine.

Three tests carry the weight:

* ``test_a_missing_document_blocks_readiness`` — a decision provider cannot
  override a missing mandatory document, at either end of the lifecycle.
* ``test_submitted_requires_a_receipt`` — an application with no funder reference
  is not submitted, and claiming otherwise is how a funder gets an application
  Granada believes it sent but did not.
* ``test_every_state_is_reachable_and_terminal_states_are_final`` — a state
  machine with an island in it is one where an application can silently stop.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import models  # noqa: E402
from agent.organisation_memory import DocumentVault, OrganisationMemory, checksum_bytes  # noqa: E402
from agent.workspace import (  # noqa: E402
    ACTIVE_STATES,
    ALL_STATES,
    ALLOWED,
    AWARDED,
    CLARIFICATION_RECEIVED,
    CLOSED,
    DISCOVERED,
    INTERVIEW,
    MATCHED,
    PREPARING,
    QUALIFIED,
    READY_TO_SUBMIT,
    REJECTED,
    REJECTED_BY_RULE,
    RESEARCHING,
    RESPONSE_PREPARING,
    RESPONSE_SENT,
    RESPONSE_WAITING_APPROVAL,
    SHORTLISTED,
    SUBMITTED,
    SUBMITTING,
    TERMINAL_STATES,
    WAITING_FOR_APPROVAL,
    WAITING_FOR_DATA,
    WITHDRAWN,
    ApplicationWorkspace,
    GuardFailed,
    IllegalTransition,
    WorkspaceError,
    WorkspaceNotFound,
    reachable_from,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'workspace.db'}", future=True)
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def org(db):
    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    row = models.Organisation(
        id=str(uuid.uuid4()), name="Uganda Health NGO", slug="ug-health", owner_user_id=user.id
    )
    db.add(row)
    db.commit()
    return row.id


def _opportunity(db, **overrides):
    payload = {
        "title": "Community Health Grant",
        "source_url": f"https://funders.example.org/{uuid.uuid4().hex[:8]}",
        "source_name": "Example Funder",
        "country": "Uganda",
        "content_hash": uuid.uuid4().hex + uuid.uuid4().hex,
        "dedupe_fingerprint": uuid.uuid4().hex + uuid.uuid4().hex,
        "is_active": True,
        "deadline": datetime.now(timezone.utc) + timedelta(days=30),
        "created_at": datetime.now(timezone.utc),
    }
    payload.update(overrides)
    row = models.Opportunity(**payload)
    db.add(row)
    db.commit()
    return row


def _ready_org(db, org):
    memory = OrganisationMemory(db, org)
    memory.record_fact(key="country", value="Uganda", state=models.OrgFact.VERIFIED, source="user:1")
    memory.record_fact(key="organisation_type", value="NGO", state=models.OrgFact.VERIFIED, source="user:1")
    memory.record_fact(
        key="registration_valid_until", value="2030-01-01", state=models.OrgFact.VERIFIED,
        source="user:1", valid_until=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    db.commit()
    return org


def _drive_to(db, org, workspace, application, target):
    """Move a workspace along a legal path to ``target``."""
    paths = {
        RESEARCHING: [MATCHED, RESEARCHING],
        PREPARING: [MATCHED, QUALIFIED, RESEARCHING, PREPARING],
        WAITING_FOR_DATA: [MATCHED, QUALIFIED, WAITING_FOR_DATA],
        WAITING_FOR_APPROVAL: [MATCHED, QUALIFIED, PREPARING, WAITING_FOR_APPROVAL],
        READY_TO_SUBMIT: [MATCHED, QUALIFIED, PREPARING, READY_TO_SUBMIT],
        SUBMITTING: [MATCHED, QUALIFIED, PREPARING, READY_TO_SUBMIT, SUBMITTING],
        SUBMITTED: [MATCHED, QUALIFIED, PREPARING, READY_TO_SUBMIT, SUBMITTING, SUBMITTED],
    }
    kwargs = {}
    for step in paths[target]:
        if (application.state, step) in {(WAITING_FOR_APPROVAL, READY_TO_SUBMIT)}:
            kwargs["approved_by"] = "user-approver"
        if (application.state, step) in {(SUBMITTING, SUBMITTED)}:
            kwargs["receipt"] = "FUNDER-REF-123"
        if step == READY_TO_SUBMIT:
            pass  # readiness is evaluated by the workspace itself
        workspace.transition(application, step, **kwargs)
    return application


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
def test_the_state_set_matches_the_brief():
    """Every state the brief names, and no invented ones."""
    required = {
        "DISCOVERED", "MATCHED", "QUALIFIED", "REJECTED_BY_RULE", "RESEARCHING",
        "PREPARING", "WAITING_FOR_DATA", "WAITING_FOR_APPROVAL", "READY_TO_SUBMIT",
        "SUBMITTING", "SUBMITTED", "CLARIFICATION_RECEIVED", "RESPONSE_PREPARING",
        "RESPONSE_WAITING_APPROVAL", "RESPONSE_SENT", "SHORTLISTED", "INTERVIEW",
        "AWARDED", "REJECTED", "WITHDRAWN", "CLOSED",
    }
    assert set(ALL_STATES) == required
    assert len(ALL_STATES) == len(set(ALL_STATES)), "duplicate state name"


def test_every_state_is_reachable_and_terminal_states_are_final():
    """An island is a state an application can never leave, silently.

    Also proves no terminal state has an outgoing edge - "AWARDED then something
    else" would make an outcome revisable, which it is not.
    """
    reachable = reachable_from(DISCOVERED) | {DISCOVERED}
    unreachable = set(ALL_STATES) - reachable
    assert unreachable == set(), f"unreachable states: {sorted(unreachable)}"

    for state in TERMINAL_STATES:
        assert ALLOWED[state] == frozenset(), f"{state} is not terminal"
    # And the converse: nothing outside the terminal set is a dead end.
    for state in ALL_STATES:
        if state not in TERMINAL_STATES:
            assert ALLOWED[state], f"{state} is a dead end but not declared terminal"


def test_every_transition_target_is_a_known_state():
    for source, targets in ALLOWED.items():
        assert source in ALL_STATES
        for target in targets:
            assert target in ALL_STATES, f"{source} -> {target} names an unknown state"


def test_active_states_exclude_the_terminal_ones():
    assert ACTIVE_STATES & TERMINAL_STATES == set()
    assert ACTIVE_STATES <= set(ALL_STATES)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
def test_create_makes_one_workspace_per_opportunity(db, org):
    """Two workspaces would mean two answers written to the same funder."""
    _ready_org(db, org)
    opportunity = _opportunity(db)
    workspace = ApplicationWorkspace(db, org)
    first = workspace.create(opportunity)
    second = workspace.create(opportunity)
    db.commit()
    assert first.id == second.id
    assert len(db.execute(select(models.Application)).scalars().all()) == 1


def test_the_database_enforces_one_workspace_per_opportunity(db, org):
    _ready_org(db, org)
    opportunity = _opportunity(db)
    db.add(models.Application(
        org_id=org, opportunity_id=opportunity.id, state=DISCOVERED, version=1,
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    db.add(models.Application(
        org_id=org, opportunity_id=opportunity.id, state=DISCOVERED, version=1,
        created_at=datetime.now(timezone.utc),
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_creation_records_the_first_transition(db, org):
    _ready_org(db, org)
    opportunity = _opportunity(db)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(opportunity)
    db.commit()
    history = workspace.history(application)
    assert len(history) == 1
    assert history[0].from_state is None
    assert history[0].to_state == DISCOVERED


def test_workspace_refuses_an_unknown_tenant(db):
    with pytest.raises(WorkspaceError) as excinfo:
        ApplicationWorkspace(db, "")
    assert "deny" in str(excinfo.value)


def test_require_raises_for_an_unknown_opportunity(db, org):
    _ready_org(db, org)
    with pytest.raises(WorkspaceNotFound):
        ApplicationWorkspace(db, org).require(str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------
def test_an_illegal_transition_is_refused_with_the_legal_ones_named(db, org):
    """A refusal has to be actionable, so it lists what would have worked."""
    _ready_org(db, org)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(_opportunity(db))
    db.commit()

    with pytest.raises(IllegalTransition) as excinfo:
        workspace.transition(application, AWARDED)
    message = str(excinfo.value)
    assert "not permitted" in message
    assert "QUALIFIED" in message or "MATCHED" in message


def test_a_terminal_state_cannot_be_escaped(db, org):
    _ready_org(db, org)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(_opportunity(db))
    workspace.transition(application, WITHDRAWN)
    db.commit()
    with pytest.raises(IllegalTransition) as excinfo:
        workspace.transition(application, MATCHED)
    assert "terminal" in str(excinfo.value)


def test_transitioning_to_the_current_state_is_idempotent(db, org):
    """A redelivered message asking for the state we are in is not an error, and
    must not append a history row."""
    _ready_org(db, org)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(_opportunity(db))
    workspace.transition(application, MATCHED)
    db.commit()
    before = len(workspace.history(application))
    workspace.transition(application, MATCHED)
    db.commit()
    assert len(workspace.history(application)) == before


def test_every_transition_appends_history_and_bumps_the_version(db, org):
    _ready_org(db, org)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(_opportunity(db))
    workspace.transition(application, MATCHED, reason="passed every hard gate")
    workspace.transition(application, QUALIFIED, reason="decision agreed")
    db.commit()

    versions = [t.version for t in workspace.history(application)]
    assert versions == [1, 2, 3]
    assert application.version == 3
    assert workspace.history(application)[1].reason == "passed every hard gate"


def test_the_history_records_who_and_how(db, org):
    """'Who did this' has a different answer and a different consequence for a
    human, an agent and the system."""
    _ready_org(db, org)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(_opportunity(db))
    workspace.transition(
        application, MATCHED,
        actor_type=models.ApplicationTransition.ACTOR_AGENT,
        job_id="job-9", decision_id="dec-4", correlation_id="corr-7",
    )
    db.commit()

    row = [t for t in workspace.history(application) if t.to_state == MATCHED][0]
    assert row.actor_type == models.ApplicationTransition.ACTOR_AGENT
    assert row.job_id == "job-9"
    assert row.decision_id == "dec-4"
    assert row.correlation_id == "corr-7"


def test_transitioning_another_organisations_workspace_is_refused(db, org):
    _ready_org(db, org)
    other_user = models.User(id=str(uuid.uuid4()), display_name="Other")
    db.add(other_user)
    db.commit()
    other = models.Organisation(
        id=str(uuid.uuid4()), name="Other", slug="other", owner_user_id=other_user.id
    )
    db.add(other)
    db.commit()

    theirs = ApplicationWorkspace(db, other.id).create(_opportunity(db))
    db.commit()
    with pytest.raises(WorkspaceError):
        ApplicationWorkspace(db, org).transition(theirs, MATCHED)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def test_a_missing_document_blocks_readiness(db, org):
    """A decision provider cannot override a missing mandatory document.

    The same rule as the hard eligibility gates, applied at the other end of the
    lifecycle: the deterministic check owns the gate.
    """
    _ready_org(db, org)
    opportunity = _opportunity(
        db, eligibility_criteria="Applicants must attach a registration certificate"
    )
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(opportunity)
    _drive_to(db, org, workspace, application, PREPARING)
    db.commit()

    report = workspace.readiness(application)
    assert report.ready is False
    assert any("registration certificate" in b for b in report.blockers)

    with pytest.raises(GuardFailed) as excinfo:
        workspace.transition(application, READY_TO_SUBMIT)
    assert "not ready" in str(excinfo.value)


def test_an_available_approved_document_permits_readiness(db, org):
    _ready_org(db, org)
    opportunity = _opportunity(
        db, eligibility_criteria="Applicants must attach a registration certificate"
    )
    vault = DocumentVault(db, org)
    document = vault.add_version(
        title="Registration", doc_type="registration_certificate",
        storage_key="org/reg.pdf", checksum_sha256=checksum_bytes(b"x"),
        mime_type="application/pdf",
        valid_until=datetime.now(timezone.utc) + timedelta(days=365),
    )
    vault.approve(document, approved_by="user-1")
    db.commit()

    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(opportunity)
    _drive_to(db, org, workspace, application, PREPARING)
    assert workspace.readiness(application).ready is True
    workspace.transition(application, READY_TO_SUBMIT)
    db.commit()
    assert application.state == READY_TO_SUBMIT


def test_an_expired_deadline_blocks_readiness(db, org):
    _ready_org(db, org)
    opportunity = _opportunity(db, deadline=datetime.now(timezone.utc) - timedelta(days=1))
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(opportunity)
    _drive_to(db, org, workspace, application, PREPARING)
    report = workspace.readiness(application)
    assert report.ready is False
    assert any("deadline passed" in b for b in report.blockers)


def test_a_missing_organisation_fact_blocks_readiness(db, org):
    """Missing material facts are a work item, and readiness says which."""
    _ready_org(db, org)
    # Remove the registration fact by superseding it with an expired one.
    OrganisationMemory(db, org).record_fact(
        key="registration_valid_until", value="2020-01-01",
        state=models.OrgFact.VERIFIED, source="user:1",
        valid_until=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    db.commit()

    opportunity = _opportunity(db)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(opportunity)
    _drive_to(db, org, workspace, application, PREPARING)
    report = workspace.readiness(application)
    assert report.ready is False
    assert any("registration_valid_until" in b for b in report.blockers)


def test_readiness_reports_every_blocker_not_just_the_first(db, org):
    """The blocker list is the work item, so stopping at the first is unhelpful."""
    OrganisationMemory(db, org).record_fact(
        key="country", value="Kenya", state=models.OrgFact.USER_PROVIDED, source="user:1"
    )
    db.commit()
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(_opportunity(db, deadline=datetime.now(timezone.utc) - timedelta(days=1)))
    _drive_to(db, org, workspace, application, PREPARING)
    report = workspace.readiness(application)
    assert len(report.blockers) >= 2


def test_waiting_for_approval_requires_a_named_approver(db, org):
    """'The system decided it was fine' is not approval."""
    _ready_org(db, org)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(_opportunity(db))
    _drive_to(db, org, workspace, application, WAITING_FOR_APPROVAL)
    db.commit()

    with pytest.raises(GuardFailed) as excinfo:
        workspace.transition(application, READY_TO_SUBMIT)
    assert "named human approver" in str(excinfo.value)

    # And with an approver it succeeds.
    workspace.transition(application, READY_TO_SUBMIT, approved_by="user-approver-1")
    db.commit()
    assert application.state == READY_TO_SUBMIT


def test_approve_records_who_and_when(db, org):
    _ready_org(db, org)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(_opportunity(db))
    _drive_to(db, org, workspace, application, WAITING_FOR_APPROVAL)
    workspace.approve(application, approved_by="user-9", reason="read the guidelines")
    db.commit()
    assert application.approved_by == "user-9"
    assert application.approved_at is not None


def test_approve_requires_a_named_person(db, org):
    _ready_org(db, org)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(_opportunity(db))
    with pytest.raises(GuardFailed):
        workspace.approve(application, approved_by="")


def test_submitted_requires_a_receipt(db, org):
    """An application with no funder reference is not submitted.

    Claiming otherwise is how a funder gets an application Granada believes it
    sent but did not - and the organisation stops watching the deadline.
    """
    _ready_org(db, org)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(_opportunity(db))
    _drive_to(db, org, workspace, application, SUBMITTING)
    db.commit()

    with pytest.raises(GuardFailed) as excinfo:
        workspace.transition(application, SUBMITTED)
    assert "external receipt" in str(excinfo.value)

    workspace.transition(application, SUBMITTED, receipt="FUNDER-REF-999")
    db.commit()
    assert application.state == SUBMITTED
    assert application.submission_receipt == "FUNDER-REF-999"
    assert application.submitted_at is not None


def test_a_receipt_recorded_earlier_is_accepted(db, org):
    """The guard accepts a receipt already on the workspace, so a retry after a
    crash does not force the caller to hold state."""
    _ready_org(db, org)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(_opportunity(db))
    _drive_to(db, org, workspace, application, SUBMITTING)
    application.submission_receipt = "FUNDER-REF-EARLIER"
    db.commit()
    workspace.transition(application, SUBMITTED)
    db.commit()
    assert application.state == SUBMITTED


def test_submitting_is_a_recoverable_state(db, org):
    """An application stuck in SUBMITTING is one a human should look at, and it
    can be driven back to READY_TO_SUBMIT rather than being a dead end."""
    _ready_org(db, org)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(_opportunity(db))
    _drive_to(db, org, workspace, application, READY_TO_SUBMIT)
    workspace.transition(application, SUBMITTING)
    workspace.transition(application, READY_TO_SUBMIT)
    db.commit()
    assert application.state == READY_TO_SUBMIT


# ---------------------------------------------------------------------------
# Response cycle and outcomes
# ---------------------------------------------------------------------------
def test_a_clarification_can_be_answered_and_approved(db, org):
    _ready_org(db, org)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(_opportunity(db))
    _drive_to(db, org, workspace, application, SUBMITTED)

    workspace.transition(application, CLARIFICATION_RECEIVED, reason="funder asked for a budget")
    workspace.transition(application, RESPONSE_PREPARING)
    workspace.transition(application, RESPONSE_WAITING_APPROVAL)
    db.commit()

    with pytest.raises(GuardFailed):
        workspace.transition(application, RESPONSE_SENT)
    workspace.transition(application, RESPONSE_SENT, approved_by="user-1")
    workspace.transition(application, SUBMITTED)
    db.commit()
    assert application.state == SUBMITTED


def test_reaching_a_terminal_state_stamps_the_outcome(db, org):
    _ready_org(db, org)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(_opportunity(db))
    _drive_to(db, org, workspace, application, SUBMITTED)
    workspace.transition(application, AWARDED, reason="funder confirmed")
    db.commit()
    assert application.outcome == AWARDED
    assert application.closed_at is not None


def test_withdrawal_is_available_from_every_active_state(db, org):
    """An organisation must always be able to stop.

    Checked structurally rather than by walking every state, because the property
    that matters is that no state traps the organisation.
    """
    for state in ACTIVE_STATES:
        assert WITHDRAWN in ALLOWED[state] or state in {SUBMITTING}, (
            f"an application in {state} cannot be withdrawn"
        )


def test_the_full_lifecycle_is_walkable(db, org):
    """A complete run from discovery to award, which is the path that has to work."""
    _ready_org(db, org)
    vault = DocumentVault(db, org)
    opportunity = _opportunity(db)
    workspace = ApplicationWorkspace(db, org)
    application = workspace.create(opportunity)

    for step in (MATCHED, QUALIFIED, RESEARCHING, PREPARING, WAITING_FOR_APPROVAL):
        workspace.transition(application, step)
    workspace.transition(application, READY_TO_SUBMIT, approved_by="user-1")
    workspace.transition(application, SUBMITTING)
    workspace.transition(application, SUBMITTED, receipt="REF-1")
    workspace.transition(application, SHORTLISTED)
    workspace.transition(application, INTERVIEW)
    workspace.transition(application, AWARDED)
    db.commit()

    assert application.state == AWARDED
    assert application.outcome == AWARDED
    assert [t.to_state for t in workspace.history(application)] == [
        DISCOVERED, MATCHED, QUALIFIED, RESEARCHING, PREPARING,
        WAITING_FOR_APPROVAL, READY_TO_SUBMIT, SUBMITTING, SUBMITTED,
        SHORTLISTED, INTERVIEW, AWARDED,
    ]


def test_active_lists_only_unfinished_workspaces(db, org):
    _ready_org(db, org)
    workspace = ApplicationWorkspace(db, org)
    live = workspace.create(_opportunity(db), state=MATCHED)
    dead = workspace.create(_opportunity(db), state=REJECTED_BY_RULE)
    db.commit()
    active_ids = {a.id for a in workspace.active()}
    assert live.id in active_ids
    assert dead.id not in active_ids
