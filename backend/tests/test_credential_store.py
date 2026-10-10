"""The credential store: encrypted secrets, and the properties that make them safe.

WHY THIS FILE EXISTS

`MailAccount.credentials_ref` pointed at a store that did not exist, so a per-organisation connection
had nowhere to keep its secret. This is that store, and the tests below are the ones that decide whether
it is encryption or theatre:

  * a payload round-trips
  * the CIPHERTEXT contains no trace of the plaintext
  * there is no working default key (a default is a key in the source tree)
  * a credential from another organisation does not resolve
  * revoking DESTROYS the secret rather than flagging it
  * a returned payload is a COPY, so a caller cannot alter what the next caller reads
  * neither the repr nor a log line can carry the secret
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.credential_store import (  # noqa: E402
    CredentialNotFound,
    CredentialStore,
    CredentialStoreError,
    CredentialStoreUnavailable,
    CredentialUndecryptable,
    generate_key,
    store_from_settings,
)


@pytest.fixture
def key() -> str:
    return generate_key()


@pytest.fixture
def db():
    """An in-memory SQLite session with the schema built.

    STATICPOOL, NOT THE DEFAULT. `create_engine("sqlite://")` gives every CONNECTION its own empty
    database, so a table created on one connection is invisible on the next - and whether the pool
    happens to reuse the same connection depends on what ran before. That made these tests pass in
    isolation and fail with 14 errors inside the full suite, which is the worst way for a test to be
    wrong. `StaticPool` pins one connection for the life of the engine.

    The same trap is already documented in `conftest.py`, next to the 125 ms/3828 ms comparison.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import models

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def org(db):
    """Two organisations, so a cross-tenant read is testable rather than assumed.

    A REAL `User` ROW, not a placeholder. `database.py` registers a GLOBAL `connect` listener that runs
    `PRAGMA foreign_keys=ON` on every SQLite connection in the process - "including engines this module
    never created" - so whether a fabricated `owner_user_id` was accepted depended on whether
    `database.py` had been imported yet. That made these tests pass alone and fail with 14 errors inside
    the full suite.

    Satisfying the constraint is the honest fix: the deployed database has that foreign key, so a test
    that only passes when it is switched off is testing a database nobody runs.
    """
    import models

    db.add(models.User(id="user-owner", display_name="Owner"))
    db.flush()
    for identifier in ("org-a", "org-b"):
        db.add(
            models.Organisation(
                id=identifier,
                name=identifier,
                slug=identifier,
                owner_user_id="user-owner",
            )
        )
    db.flush()
    return "org-a"


# ===========================================================================
# THE KEY
# ===========================================================================
def test_no_key_refuses_to_construct():
    """A default would mean a deployment that never set one still 'encrypts' - with a value in the
    source tree, which is not encryption. Refusing is the only honest option."""
    with pytest.raises(CredentialStoreUnavailable):
        CredentialStore(db=None, key="")


def test_an_invalid_key_is_refused():
    with pytest.raises(CredentialStoreUnavailable):
        CredentialStore(db=None, key="not-a-fernet-key")


def test_store_from_settings_raises_when_unconfigured():
    class S:
        credential_encryption_key = ""

    with pytest.raises(CredentialStoreUnavailable):
        store_from_settings(None, S())


def test_a_generated_key_is_accepted(db, key):
    assert CredentialStore(db, key=key) is not None


# ===========================================================================
# ROUND TRIP, AND WHAT IS ACTUALLY STORED
# ===========================================================================
def test_a_payload_round_trips(db, org, key):
    store = CredentialStore(db, key=key)
    store.put(
        org_id=org,
        ref="mail:account:1",
        payload={"password": "hunter2", "username": "grants@example.org"},
    )
    assert store.get(org_id=org, ref="mail:account:1") == {
        "password": "hunter2",
        "username": "grants@example.org",
    }


def test_the_stored_ciphertext_contains_no_trace_of_the_plaintext(db, org, key):
    """THE test. Anything less than this is obfuscation with extra steps."""
    import models

    store = CredentialStore(db, key=key)
    secret = "hunter2-very-distinctive-value"
    store.put(org_id=org, ref="r", payload={"password": secret})

    row = db.query(models.CredentialSecret).filter_by(org_id=org, ref="r").one()
    assert secret not in row.ciphertext
    assert "password" not in row.ciphertext
    assert row.ciphertext and row.ciphertext != secret


def test_a_returned_payload_is_a_copy(db, org, key):
    """A caller that mutates what it is handed must not change what the next caller receives - a
    credential that varies by call order is a bug nobody would find."""
    store = CredentialStore(db, key=key)
    store.put(org_id=org, ref="r", payload={"password": "original"})

    first = store.get(org_id=org, ref="r")
    first["password"] = "tampered"
    assert store.get(org_id=org, ref="r")["password"] == "original"


def test_an_empty_payload_is_refused(db, org, key):
    """Storing nothing is always a mistake, and it would silently replace a working credential."""
    with pytest.raises(CredentialStoreError):
        CredentialStore(db, key=key).put(org_id=org, ref="r", payload={})


def test_a_missing_ref_is_refused(db, org, key):
    with pytest.raises(CredentialStoreError):
        CredentialStore(db, key=key).put(org_id=org, ref="", payload={"a": 1})


def test_a_missing_org_is_refused(db, key):
    """An organisation-less credential would be readable by nothing and would defeat the unique
    constraint's tenancy."""
    with pytest.raises(CredentialStoreError):
        CredentialStore(db, key=key).put(org_id="", ref="r", payload={"a": 1})


# ===========================================================================
# TENANT ISOLATION
# ===========================================================================
def test_another_organisations_ref_does_not_resolve(db, org, key):
    """A ref appears in logs far more casually than a credential ever should, so a guessed ref must
    resolve to nothing."""
    store = CredentialStore(db, key=key)
    store.put(org_id="org-b", ref="mail:account:shared-name", payload={"password": "b-secret"})

    with pytest.raises(CredentialNotFound):
        store.get(org_id="org-a", ref="mail:account:shared-name")


def test_the_same_ref_in_two_organisations_is_two_credentials(db, org, key):
    """Uniqueness is (org_id, ref). A global ref would let one organisation's setup collide with
    another's, and the second write would silently overwrite the first."""
    store = CredentialStore(db, key=key)
    store.put(org_id="org-a", ref="mail:account:1", payload={"password": "a"})
    store.put(org_id="org-b", ref="mail:account:1", payload={"password": "b"})

    assert store.get(org_id="org-a", ref="mail:account:1")["password"] == "a"
    assert store.get(org_id="org-b", ref="mail:account:1")["password"] == "b"


def test_listing_is_scoped_to_one_organisation(db, org, key):
    store = CredentialStore(db, key=key)
    store.put(org_id="org-a", ref="a1", payload={"p": "1"})
    store.put(org_id="org-b", ref="b1", payload={"p": "2"})

    refs = {record.ref for record in store.list_refs(org_id="org-a")}
    assert refs == {"a1"}


# ===========================================================================
# REVOCATION DESTROYS RATHER THAN FLAGS
# ===========================================================================
def test_revoking_destroys_the_ciphertext(db, org, key):
    """A revoked credential that can still be decrypted is one a bug can resurrect. 'Revoked' that
    depends on every reader checking a flag is not revocation, it is a convention."""
    import models

    store = CredentialStore(db, key=key)
    store.put(org_id=org, ref="r", payload={"password": "secret"})

    assert store.revoke(org_id=org, ref="r") is True
    row = db.query(models.CredentialSecret).filter_by(org_id=org, ref="r").one()
    assert row.ciphertext == "", "the ciphertext survived revocation"
    assert row.status == models.CredentialSecret.REVOKED
    with pytest.raises(CredentialNotFound):
        store.get(org_id=org, ref="r")


def test_revoking_something_absent_reports_false(db, org, key):
    assert CredentialStore(db, key=key).revoke(org_id=org, ref="never-existed") is False


# ===========================================================================
# KEY ROTATION IS A DIFFERENT FAILURE FROM ABSENCE
# ===========================================================================
def test_the_wrong_key_is_undecryptable_not_missing(db, org, key):
    """The two demand different responses: a missing credential is configuration, while this is a
    key-rotation incident and every credential under the old key is now unreadable. Collapsing them
    would make a rotation look like a deployment mistake."""
    store = CredentialStore(db, key=key)
    store.put(org_id=org, ref="r", payload={"password": "secret"})

    other = CredentialStore(db, key=generate_key())
    with pytest.raises(CredentialUndecryptable):
        other.get(org_id=org, ref="r")


def test_rotation_replaces_the_credential_and_records_when(db, org, key):
    """`rotated_at` is kept rather than overwritten, so 'when did the previous one stop working' stays
    answerable after the fact."""
    import models

    store = CredentialStore(db, key=key)
    store.put(org_id=org, ref="r", payload={"password": "first"})
    store.put(org_id=org, ref="r", payload={"password": "second"})

    assert store.get(org_id=org, ref="r")["password"] == "second"
    row = db.query(models.CredentialSecret).filter_by(org_id=org, ref="r").one()
    assert row.rotated_at is not None
    assert db.query(models.CredentialSecret).filter_by(org_id=org, ref="r").count() == 1, (
        "rotation inserted a second row; which credential is in use would then depend on row order"
    )


# ===========================================================================
# THE SECRET MUST NOT ESCAPE THROUGH A LOG OR A REPR
# ===========================================================================
def test_the_repr_carries_neither_the_key_nor_a_payload(db, key):
    """The default repr of an object holding a key is a habit, and a habit is how a key reaches a log
    line."""
    text = repr(CredentialStore(db, key=key))
    assert key not in text
    assert "redacted" in text


def test_describe_returns_metadata_only(db, org, key):
    """An operator listing connections must not be able to print a credential by accident, so the
    listing type has nowhere to put one."""
    store = CredentialStore(db, key=key)
    store.put(org_id=org, ref="r", payload={"password": "distinctive-secret"})

    record = store.describe(org_id=org, ref="r")
    assert not hasattr(record, "payload")
    assert "distinctive-secret" not in repr(record)


def test_logging_a_store_operation_does_not_write_the_secret(db, org, key, caplog):
    """`put` logs. If it ever logged the payload, every send would write a credential into the log
    aggregator - which is read by far more people than the database."""
    with caplog.at_level(logging.DEBUG):
        CredentialStore(db, key=key).put(
            org_id=org, ref="r", payload={"password": "distinctive-secret"}
        )
    for record in caplog.records:
        assert "distinctive-secret" not in record.getMessage()
        assert "distinctive-secret" not in str(getattr(record, "__dict__", {}))
