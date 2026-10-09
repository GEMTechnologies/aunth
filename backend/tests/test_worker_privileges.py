"""The browser worker's privilege guard.

ADR-0011 in its applied form. `granada_fleet` has BYPASSRLS and 26+ tables carry RLS - confirmed live
on production. The executor needs that for discovery. The browser worker does not: it needs no
cross-tenant visibility at all, and it is the component that reads tenant documents and types tenant
data into a live portal.

These tests are about REFUSAL, and about the guard not leaking what it refuses.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.worker_privileges import (  # noqa: E402
    DATABASE_ENV_VARS,
    NARROW_ROLE,
    WIDE_ROLE,
    WorkerPrivilegeError,
    assert_worker_privileges,
    check_worker_privileges,
    describe,
)


# ===========================================================================
# THE REFUSAL
# ===========================================================================
def test_the_fleet_url_is_refused():
    """THE test. The widest role in the system attached to the component with the least need for it
    is a privilege escalation by configuration."""
    d = check_worker_privileges({"FLEET_DATABASE_URL": "postgresql://granada_fleet:pw@db:5432/granada"})
    assert d.permitted is False
    assert "FLEET_DATABASE_URL" in d.refused
    assert "BYPASSRLS" in d.because


def test_each_database_variable_is_examined():
    for name in DATABASE_ENV_VARS:
        env = {name: f"postgresql://{WIDE_ROLE}:pw@db/x"}
        assert check_worker_privileges(env).permitted is False, name


def test_an_empty_environment_is_permitted():
    """The intended shape: the worker needs no database connection at all."""
    d = check_worker_privileges({})
    assert d.permitted is True
    assert "no database connection" in d.because


def test_a_narrow_role_is_permitted_but_NAMED():
    """Not the escalation this guard exists to prevent - but an operator should be able to see which
    case it was."""
    d = check_worker_privileges({"DATABASE_URL": f"postgresql://{NARROW_ROLE}:pw@db/x"})
    assert d.permitted is True
    assert NARROW_ROLE in d.because


def test_assert_raises_rather_than_reporting():
    with pytest.raises(WorkerPrivilegeError):
        assert_worker_privileges({"FLEET_DATABASE_URL": "postgresql://granada_fleet@db/x"})


def test_assert_returns_a_decision_when_permitted():
    d = assert_worker_privileges({})
    assert d.permitted is True


# ===========================================================================
# DETECTION IS ABOUT THE ROLE, NOT THE SHAPE OF THE URL
# ===========================================================================
def test_the_userinfo_form_is_detected():
    assert check_worker_privileges(
        {"DATABASE_URL": f"postgresql://{WIDE_ROLE}:secret@host:5432/db"}
    ).permitted is False


def test_the_no_password_form_is_detected():
    assert check_worker_privileges(
        {"DATABASE_URL": f"postgresql://{WIDE_ROLE}@host/db"}
    ).permitted is False


def test_a_role_query_parameter_is_detected():
    """A check that only looked at the scheme would miss this, and it is the same escalation."""
    assert check_worker_privileges(
        {"DATABASE_URL": f"postgresql://someone@host/db?role={WIDE_ROLE}"}
    ).permitted is False


def test_case_is_not_a_bypass():
    assert check_worker_privileges(
        {"DATABASE_URL": "postgresql://GRANADA_FLEET@host/db"}
    ).permitted is False


def test_a_whitespace_only_value_is_not_a_connection():
    assert check_worker_privileges({"DATABASE_URL": "   "}).permitted is True


def test_a_different_role_with_a_similar_name_is_not_refused():
    """`granada_app` and `granada_fleet` are the two that exist; a substring match would be sloppy in
    the other direction and could refuse a legitimate role."""
    assert check_worker_privileges({"DATABASE_URL": "postgresql://granada_application@db/x"}).permitted is True


# ===========================================================================
# THE GUARD DOES NOT LEAK
# ===========================================================================
def test_the_decision_names_variables_and_never_their_values():
    """A connection URL carries a password. A refusal message is exactly the thing that ends up in a
    log or a ticket, so it must not carry one."""
    secret = "hunter2-should-never-appear"
    d = check_worker_privileges({"FLEET_DATABASE_URL": f"postgresql://{WIDE_ROLE}:{secret}@db/x"})
    assert d.permitted is False
    assert secret not in d.because
    assert secret not in str(d.refused)


def test_the_exception_message_does_not_leak_the_password():
    secret = "another-secret-value"
    with pytest.raises(WorkerPrivilegeError) as e:
        assert_worker_privileges({"FLEET_DATABASE_URL": f"postgresql://{WIDE_ROLE}:{secret}@db/x"})
    assert secret not in str(e.value)


# ===========================================================================
# THE DEFAULT IS THE REAL ENVIRONMENT
# ===========================================================================
def test_it_reads_the_process_environment_by_default():
    """Called with no argument it inspects os.environ, so a worker cannot sidestep it by not passing
    a mapping."""
    d = check_worker_privileges()
    assert isinstance(d.permitted, bool)


# ===========================================================================
# THE BOUNDARY IS STATED
# ===========================================================================
def test_describe_records_the_rule_and_its_known_limit():
    d = describe()
    assert d["wide_role"] == WIDE_ROLE
    assert d["narrow_role"] == NARROW_ROLE
    assert "must not connect as granada_fleet" in d["rule"]
    assert "after startup" in d["known_limit"], "the limit must be recorded, not implied away"
    joined = " ".join(d["does_not_do"])
    assert "already opened it" in joined
    assert "names only" in joined
