"""The link from a delivered message to the Granada mailbox it arrived at.

An organisation holds one mailbox PER PORTAL ACCOUNT, each with its own unpredictable address. If an
inbound message is not labelled with the mailbox it arrived at, those histories merge: the codes,
notifications and recovery links of three portals sit in one undifferentiated list, and a code for one
portal looks exactly like a code for another.

`mail_account_id` cannot express the distinction. It names the EXTERNAL account that carried the
message - the Cloudflare delivery, a connected Gmail - and one such account carries mail for every
mailbox Granada manages. That is the whole reason `mail_identity_id` was added.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import mail_ingest_api
import models
from agent.mail.service import GranadaMail

KEY = "test-ingest-key-0123456789"

ORG = "org-1"
MAILBOX = "ngo-a-1a2b3c4d@granadac.com"
IDENTITY = "identity-9"
AGENT = "agent-7"


# ---------------------------------------------------------------------------
# Fakes: just enough of a session for the questions ingest actually asks
# ---------------------------------------------------------------------------
class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0][0] if self._rows else None

    def all(self):
        return list(self._rows)


class _Query:
    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return []


class _Session:
    """Answers the routing lookup, the agent lookup, and tolerates the tenant bind."""

    def __init__(self, *, route, identities=()):
        self.route = route
        self.identities = list(identities)
        self.added = []
        self.committed = False
        self.closed = False

    def execute(self, statement, params=None):
        sql = str(statement)
        if "mail_routing" in sql:
            return _Rows([self.route])
        if "mail_identities" in sql:
            return _Rows([(AGENT,)])
        # `set_config` / SET LOCAL from the tenant binding, and anything else.
        return _Rows([])

    def query(self, *args, **kwargs):
        return _Query()

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class _Outcome:
    reason = "no verification request is waiting for this mailbox"
    kind = None


class _Examination:
    outcome = _Outcome()
    satisfied = False
    already_satisfied = False


class _StubVerification:
    """Stands in for the verifier so this file tests the STORAGE of the mailbox, not matching."""

    def __init__(self, db, *, org_id):
        self.db = db
        self.org_id = org_id

    def examine(self, **kwargs):
        return _Examination()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(mail_ingest_api.router, prefix="/api/v1")
    return TestClient(app)


def _configure(monkeypatch, session):
    monkeypatch.setattr(config.settings, "mail_ingest_key", KEY, raising=False)
    monkeypatch.setattr(mail_ingest_api, "SESSION_FACTORY", lambda: session)

    import tenant_context

    monkeypatch.setattr(tenant_context, "set_tenant", lambda *a, **k: None)
    monkeypatch.setattr(tenant_context, "clear_tenant", lambda *a, **k: None)

    from agent.mail import verification_service

    monkeypatch.setattr(verification_service, "VerificationService", _StubVerification)


# ---------------------------------------------------------------------------
# The ingest path labels the message with the routed mailbox
# ---------------------------------------------------------------------------
def test_a_delivered_message_records_the_mailbox_it_arrived_at(client, monkeypatch):
    session = _Session(route=(ORG, IDENTITY, True))
    _configure(monkeypatch, session)

    body = {
        "to": MAILBOX,
        "sender": "no-reply@mail.unpartnerportal.org",
        "subject": "Verify your UN Partner Portal account",
        "text": "Your verification code is 123456",
        "message_id": "<m1@mail.unpartnerportal.org>",
    }
    out = client.post(
        "/api/v1/internal/mail/ingest", json=body, headers={"X-Ingest-Key": KEY}
    ).json()

    assert out["action"] == "accept"
    stored = [o for o in session.added if isinstance(o, models.MailMessage)]
    assert len(stored) == 1, "the message must be retained before anything interprets it"
    assert stored[0].mail_identity_id == IDENTITY, (
        "without this the organisation's portal mailboxes merge into one inbox"
    )
    assert stored[0].org_id == ORG
    assert session.committed is True
    assert session.closed is True, "the ingest session must not leak"


# ---------------------------------------------------------------------------
# Resolving an address back to a mailbox, for providers that report recipients
# ---------------------------------------------------------------------------
class _ServiceStub:
    def __init__(self, db, org_id=ORG):
        self.db = db
        self.org_id = org_id


class _IdentitySession:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *args, **kwargs):
        return _Rows(self._rows)


class _IdentityRow:
    def __init__(self, address, id):
        self.address = address
        self.id = id


def test_a_recipient_resolves_to_its_mailbox_ignoring_case():
    db = _IdentitySession([_IdentityRow("Ngo-A-1A2B@granadac.com", "id-1")])
    assert GranadaMail._identity_for(_ServiceStub(db), ["ngo-a-1a2b@granadac.com"]) == "id-1"


def test_an_underscore_in_an_address_is_not_a_wildcard():
    """`ILIKE` would match `john_doe@` against `johnXdoe@`, filing one mailbox's mail under another's.
    An underscore is a legal character in a local part, so the comparison must be exact."""
    db = _IdentitySession([_IdentityRow("john_doe@granadac.com", "id-1")])
    assert GranadaMail._identity_for(_ServiceStub(db), ["johnXdoe@granadac.com"]) is None


def test_an_address_matching_no_mailbox_is_left_unlabelled():
    """None rather than a guess: an unlabelled row is honest, a wrongly-labelled one mis-files mail."""
    db = _IdentitySession([_IdentityRow("ngo-a@granadac.com", "id-1")])
    assert GranadaMail._identity_for(_ServiceStub(db), ["someone-else@granadac.com"]) is None


def test_a_recipient_with_no_at_sign_is_ignored():
    db = _IdentitySession([_IdentityRow("ngo-a@granadac.com", "id-1")])
    assert GranadaMail._identity_for(_ServiceStub(db), ["not-an-address"]) is None


def test_the_first_matching_recipient_wins():
    db = _IdentitySession([
        _IdentityRow("first@granadac.com", "id-1"),
        _IdentityRow("second@granadac.com", "id-2"),
    ])
    assert GranadaMail._identity_for(_ServiceStub(db), ["second@granadac.com"]) == "id-2"


# ---------------------------------------------------------------------------
# The schema, and the migration that carries it
# ---------------------------------------------------------------------------
def test_the_message_table_carries_a_mailbox_reference():
    column = models.MailMessage.__table__.c["mail_identity_id"]
    assert column.nullable is True, "historical rows have no mailbox and must not block a deploy"
    assert column.index is True
    assert any(
        fk.target_fullname == "mail_identities.id" for fk in column.foreign_keys
    ), "a mailbox reference that points at nothing is worse than none"


def test_the_migration_chains_from_the_routing_revision():
    """Loads the revision by path, as Alembic does. A typo in the chain is a failed upgrade, and it
    is far cheaper to catch here than against the production database."""
    import importlib.util
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "024_mail_message_identity.py"
    )
    spec = importlib.util.spec_from_file_location("migration_024", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "024_mail_message_identity"
    assert module.down_revision == "023_mail_routing"
    assert callable(module.upgrade) and callable(module.downgrade)
