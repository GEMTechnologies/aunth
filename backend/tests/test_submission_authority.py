"""The submission authority gate. Every test here is about a REFUSAL.

Authority to submit is the one piece of Granada that can cause an irreversible external effect, so
the interesting cases are the ones that must NOT be allowed. A passing test that only proves the
happy path would leave the dangerous half unverified.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.submission_authority import (  # noqa: E402
    DEFAULT_GRANTED_SCOPES,
    SCOPE_PREPARE,
    SCOPE_SUBMIT,
    STATUS_ACTIVE,
    STATUS_REVOKED,
    SUBMISSION_ENABLED_SETTING,
    AuthorityGrant,
    describe,
    evaluate,
    submission_policy,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
ORG = "org-aaaa"
OTHER = "org-bbbb"


def grant(**over) -> AuthorityGrant:
    base = dict(
        org_id=ORG,
        scope=SCOPE_SUBMIT,
        granted_by="user-1",
        granted_at=NOW - timedelta(days=1),
        permission_used="submission.authorise",
    )
    base.update(over)
    return AuthorityGrant(**base)  # type: ignore[arg-type]


# ===========================================================================
# PREPARE and SUBMIT are genuinely different authorities
# ===========================================================================
def test_prepare_is_granted_by_default_and_submit_is_not():
    """The directive's central separation. Assembling a package reads the organisation's own records
    and produces something they inspect; submitting hands it to a third party."""
    prepare = evaluate(org_id=ORG, scope=SCOPE_PREPARE, grants=[], now=NOW)
    submit = evaluate(org_id=ORG, scope=SCOPE_SUBMIT, grants=[], now=NOW)

    assert prepare.allowed is True
    assert submit.allowed is False
    assert "granted no authority" in submit.reason


def test_document_preparation_permission_does_not_confer_submission():
    """A grant for PREPARE must not leak into SUBMIT by pattern-matching or by 'same organisation'."""
    grants = [grant(scope=SCOPE_PREPARE)]
    assert evaluate(org_id=ORG, scope=SCOPE_PREPARE, grants=grants, now=NOW).allowed is True
    assert evaluate(org_id=ORG, scope=SCOPE_SUBMIT, grants=grants, now=NOW).allowed is False


# ===========================================================================
# A general login is not an authorisation
# ===========================================================================
def test_an_anonymous_grant_is_not_a_grant():
    """`Workspace.REQUIRES_APPROVAL` refuses an unnamed approver for the same reason: authority has
    to attach to somebody who can be asked about it later."""
    d = evaluate(org_id=ORG, scope=SCOPE_SUBMIT, grants=[grant(granted_by="")], now=NOW)
    assert d.allowed is False
    assert "names no authorising person" in str(d.evidence.get("refusals"))


# ===========================================================================
# Expiry and revocation
# ===========================================================================
def test_an_expired_grant_does_not_authorise():
    d = evaluate(
        org_id=ORG, scope=SCOPE_SUBMIT, grants=[grant(expires_at=NOW - timedelta(seconds=1))], now=NOW
    )
    assert d.allowed is False
    assert "expired" in str(d.evidence["refusals"])


def test_a_grant_expiring_exactly_now_is_not_valid():
    """Boundary: expiry is inclusive of the instant, so a grant cannot be used at the moment it
    lapses."""
    assert evaluate(
        org_id=ORG, scope=SCOPE_SUBMIT, grants=[grant(expires_at=NOW)], now=NOW
    ).allowed is False


def test_a_revoked_grant_does_not_authorise():
    d = evaluate(
        org_id=ORG,
        scope=SCOPE_SUBMIT,
        grants=[grant(status=STATUS_REVOKED, revoked_at=NOW - timedelta(hours=1))],
        now=NOW,
    )
    assert d.allowed is False
    assert STATUS_REVOKED in str(d.evidence["refusals"])


def test_a_grant_with_no_expiry_IS_valid():
    """Absence of an expiry is a decision a human made, and is not the same as an unknown value."""
    assert evaluate(org_id=ORG, scope=SCOPE_SUBMIT, grants=[grant()], now=NOW).allowed is True


def test_a_naive_timestamp_does_not_crash_and_does_not_allow_by_accident():
    """A datetime read back without tzinfo compared against an aware `now()` raises TypeError. If a
    caller caught that broadly, a crash would become an allow."""
    naive = datetime(2026, 10, 1, 12, 0)
    assert evaluate(org_id=ORG, scope=SCOPE_SUBMIT, grants=[grant(granted_at=naive)], now=NOW).allowed is True
    assert evaluate(
        org_id=ORG, scope=SCOPE_SUBMIT, grants=[grant(expires_at=datetime(2026, 10, 1, 12, 0))], now=NOW
    ).allowed is False


# ===========================================================================
# Tenant isolation
# ===========================================================================
def test_another_organisations_grant_does_not_authorise_us():
    """NGO A's authority must never authorise NGO B. This is the whole point of per-organisation
    grants, and it is asserted by test rather than by reading the query."""
    d = evaluate(org_id=OTHER, scope=SCOPE_SUBMIT, grants=[grant(org_id=ORG)], now=NOW)
    assert d.allowed is False
    assert "granted no authority" in d.reason


def test_an_empty_org_id_is_refused():
    assert evaluate(org_id="", scope=SCOPE_SUBMIT, grants=[grant()], now=NOW).allowed is False


# ===========================================================================
# The grant must cover THIS package version
# ===========================================================================
def test_authority_for_one_package_version_does_not_cover_a_changed_package():
    """The reason `MailApproval` records a fingerprint rather than a boolean: "was the thing this
    person authorised the thing we are about to send?"

    An authority granted while the package said one thing must not silently cover a package whose
    documents have since changed.
    """
    d = evaluate(
        org_id=ORG,
        scope=SCOPE_SUBMIT,
        grants=[grant()],
        package_fingerprint="fp-after-edit",
        authorised_fingerprint="fp-when-approved",
        now=NOW,
    )
    assert d.allowed is False
    assert "changed since authority was granted" in d.reason


def test_a_matching_fingerprint_is_authorised():
    d = evaluate(
        org_id=ORG,
        scope=SCOPE_SUBMIT,
        grants=[grant()],
        package_fingerprint="fp-same",
        authorised_fingerprint="fp-same",
        now=NOW,
    )
    assert d.allowed is True


def test_an_unestablishable_authorised_fingerprint_is_refused():
    """If we cannot show WHICH version was authorised, we cannot show the authority covers what we
    are about to send - so we do not proceed."""
    d = evaluate(
        org_id=ORG,
        scope=SCOPE_SUBMIT,
        grants=[grant()],
        package_fingerprint="fp-x",
        authorised_fingerprint=None,
        now=NOW,
    )
    assert d.allowed is False
    assert "could not be established" in d.reason


# ===========================================================================
# Unknown scopes
# ===========================================================================
def test_an_unknown_scope_is_refused_not_treated_as_harmless():
    """A typo in a scope string must not become an authorisation."""
    assert evaluate(org_id=ORG, scope="SUBMIT_EXTERNAL", grants=[grant()], now=NOW).allowed is False
    assert evaluate(org_id=ORG, scope="", grants=[grant()], now=NOW).allowed is False


def test_default_granted_scopes_is_an_allowlist_not_a_denylist():
    """A future scope added by someone else must be denied until deliberately placed here."""
    assert DEFAULT_GRANTED_SCOPES == frozenset({SCOPE_PREPARE})
    assert "SUBMIT" not in DEFAULT_GRANTED_SCOPES


# ===========================================================================
# THE POLICY GATE
# ===========================================================================
def test_the_policy_gate_is_disabled_by_default():
    """Built, tested, switched off - the same shape as `autonomous_mail_enabled = False`."""
    d = submission_policy(org_id=ORG, grants=[grant()], settings={})
    assert d.allowed is False
    assert "disabled by policy" in d.reason


def test_the_policy_gate_requires_BOTH_the_flag_and_the_authority():
    """Enabling the capability does not authorise an organisation, and an organisation's authority
    does not enable the capability."""
    enabled_no_authority = submission_policy(
        org_id=ORG, grants=[], settings={SUBMISSION_ENABLED_SETTING: True}
    )
    assert enabled_no_authority.allowed is False

    authority_not_enabled = submission_policy(
        org_id=ORG, grants=[grant()], settings={SUBMISSION_ENABLED_SETTING: False}
    )
    assert authority_not_enabled.allowed is False

    both = submission_policy(
        org_id=ORG,
        grants=[grant()],
        package_fingerprint="fp",
        authorised_fingerprint="fp",
        settings={SUBMISSION_ENABLED_SETTING: True},
        now=NOW,
    )
    assert both.allowed is True


def test_the_policy_gate_refuses_on_the_flag_before_examining_the_paperwork():
    """A live submission must not depend on an organisation's paperwork being in order before we
    notice the capability is switched off."""
    d = submission_policy(org_id=ORG, grants=[], settings={})
    assert d.evidence["setting"] == SUBMISSION_ENABLED_SETTING


# ===========================================================================
# The boundary is stated, not implied
# ===========================================================================
def test_describe_states_that_it_does_not_replace_the_existing_gates():
    """This module must not be mistaken for the receipt requirement or the readiness check. A
    reviewer reading `describe()` should see the boundary without reading the surrounding code."""
    d = describe()
    assert d["fails_closed"] is True
    joined = " ".join(d["does_not_replace"])
    assert "REQUIRES_APPROVAL" in joined
    assert "REQUIRES_RECEIPT" in joined
    assert "readiness" in joined
    assert d["replaces"] == []


def test_granting_authority_is_not_submission():
    """The directive's distinction, encoded where a reader will find it."""
    assert "not submission" in describe()["granting_authority_is_not_submission"].lower() or (
        "only a confirmed external receipt" in describe()["granting_authority_is_not_submission"]
    )


# ===========================================================================
# The happy path, once, so the refusals are not the only thing proven
# ===========================================================================
def test_an_active_unexpired_grant_records_its_evidence():
    d = evaluate(org_id=ORG, scope=SCOPE_SUBMIT, grants=[grant()], now=NOW)
    assert d.allowed is True
    assert d.grant is not None
    assert d.evidence["granted_by"] == "user-1"
    assert d.evidence["permission_used"] == "submission.authorise"
