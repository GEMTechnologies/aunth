"""Connecting the organisation's OWN mailbox over IMAP.

THE GAP THIS CLOSES. The product promised "organisations should also have the option of connecting
their existing Gmail or organisational email rather than using a Granada-managed address". The IMAP
provider, the credential store and the sync scheduler were all built; **nothing created an account**,
so `mail_accounts` stayed at zero and the promise was unreachable from any client.

It matters beyond convenience: reading the organisation's own inbox is the one path that does not
depend on Cloudflare Email Routing. A verification code sent to their Gmail is found by the same
verification engine, so this is a route to a completed portal signup with no DNS change at all.

THE ASSERTION THAT IS NOT NEGOTIABLE: the password never lands in a column. It goes into the encrypted
credential store and only a reference reaches `mail_accounts.credentials_ref` - the table has no column
that could hold it, and `add_account` refuses to accept one.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import config  # noqa: E402
import models  # noqa: E402
from agent.credential_store import generate_key  # noqa: E402
from agent.decision.policy import Autonomy  # noqa: E402
from agent.granada_agent import GranadaAgentService  # noqa: E402
from database import get_db  # noqa: E402
from router import get_current_user, get_tenant_context  # noqa: E402
from tenant_context import TenantContext  # noqa: E402

PASSWORD = "app-password-not-the-account-password"


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "connectmail.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def world(db, monkeypatch):
    monkeypatch.setattr(config.settings, "credential_encryption_key", generate_key(), raising=False)

    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    org = models.Organisation(
        id=str(uuid.uuid4()), name="Granada Test Organisation",
        slug=f"org-{uuid.uuid4().hex[:8]}", owner_user_id=user.id,
    )
    db.add(org)
    db.commit()

    service = GranadaAgentService(db, org.id)
    service.provision(autonomy=Autonomy.MONITOR_ONLY)
    db.commit()
    return {"org": org, "user": user, "agent": service.get()}


@pytest.fixture
def client(db, world):
    import main

    org_id = str(world["org"].id)
    user = world["user"]

    def _db_override():
        yield db

    def _tenant_override():
        return TenantContext(user_id=user.id, org_ids=(org_id,), primary_org_id=org_id)

    main.app.dependency_overrides[get_db] = _db_override
    main.app.dependency_overrides[get_tenant_context] = _tenant_override
    main.app.dependency_overrides[get_current_user] = lambda: user
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def body(**overrides):
    payload = {
        "address": "NGO.Team@example.org",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_user": "ngo.team@example.org",
        "imap_password": PASSWORD,
        "use_ssl": True,
        "display_name": "NGO Team",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_a_mailbox_is_connected(client, db, world):
    resp = client.post("/api/v1/agent/mail/accounts", json=body())
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["connection_type"] == models.MailAccount.CONNECTION_CONNECTED_IMAP
    assert out["provider"] == "imap"

    account = db.query(models.MailAccount).first()
    assert account is not None
    # Lower-cased on the way in: two organisations must not be able to hold "NGO@x" and "ngo@x".
    assert account.address == "ngo.team@example.org"
    assert account.credentials_ref and account.credentials_ref.startswith("mail/imap/")
    # The credential is a REFERENCE, and there is no column that could hold the password.
    assert not hasattr(account, "password")


def test_the_password_is_encrypted_and_never_readable_from_the_row(client, db, world):
    client.post("/api/v1/agent/mail/accounts", json=body())
    secret = db.query(models.CredentialSecret).first()

    assert secret is not None
    assert secret.kind == models.CredentialSecret.KIND_IMAP_PASSWORD
    assert PASSWORD not in (secret.ciphertext or ""), (
        "the password must not be recoverable from the stored ciphertext"
    )

    # And it round-trips through the store, so the mailbox can actually be read.
    from agent.credential_store import store_from_settings

    account = db.query(models.MailAccount).first()
    payload = store_from_settings(db, config.settings).get(
        org_id=str(world["org"].id), ref=account.credentials_ref
    )
    assert payload["host"] == "imap.gmail.com"
    assert payload["password"] == PASSWORD


def test_a_connected_mailbox_gets_a_connected_identity(client, db, world):
    """Inbound routing resolves a recipient to an organisation through `mail_identities`, so without
    the identity the mail pulled from the organisation's own inbox would have no mailbox to file it
    under."""
    client.post("/api/v1/agent/mail/accounts", json=body())

    identity = db.query(models.MailIdentity).filter(
        models.MailIdentity.address == "ngo.team@example.org"
    ).first()
    assert identity is not None
    assert identity.identity_type == models.MailIdentity.TYPE_CONNECTED
    assert identity.is_primary is False, (
        "an external mailbox must not silently become the organisation's public address"
    )


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def test_the_same_address_cannot_be_connected_twice(client):
    assert client.post("/api/v1/agent/mail/accounts", json=body()).status_code == 201
    again = client.post("/api/v1/agent/mail/accounts", json=body())
    assert again.status_code == 409
    assert "already connected" in again.json()["detail"]


def test_an_address_with_no_at_sign_is_refused(client):
    resp = client.post("/api/v1/agent/mail/accounts", json=body(address="not-an-address"))
    assert resp.status_code == 400


def test_without_an_agent_the_mailbox_is_refused_rather_than_orphaned(client, db, world, monkeypatch):
    """A mailbox belongs to an agent. Rows saved before the check existed would be unreachable for
    ever - a mailbox that never syncs produces no error, only an absence.

    The lookup is stubbed rather than the agent deleted: `agent_specialists` references it, so a
    delete fails on the foreign key and the test would be measuring the schema, not the guard.
    """
    import agent_api

    monkeypatch.setattr(agent_api, "_agent_for", lambda db, org_id: None)

    resp = client.post("/api/v1/agent/mail/accounts", json=body())
    assert resp.status_code == 409
    assert "no Granada agent" in resp.json()["detail"]
    assert db.query(models.MailAccount).count() == 0


def test_an_unavailable_credential_store_saves_nothing(client, db, world, monkeypatch):
    """A mailbox registered without a working credential would look connected and fetch nothing."""
    monkeypatch.setattr(config.settings, "credential_encryption_key", "", raising=False)

    resp = client.post("/api/v1/agent/mail/accounts", json=body())
    assert resp.status_code == 503
    assert db.query(models.MailAccount).count() == 0
    assert db.query(models.CredentialSecret).count() == 0


def test_a_missing_password_is_refused_by_the_schema(client):
    resp = client.post("/api/v1/agent/mail/accounts", json=body(imap_password=""))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# The scheduler: a connected mailbox must actually be queued
# ---------------------------------------------------------------------------
def test_a_connected_mailbox_is_scheduled_for_sync(db, world):
    """MEASURED ON PRODUCTION, and it is why this test exists.

    A mailbox was connected over IMAP with its own encrypted credential, and the scheduler queued
    NOTHING for it - on every sweep, for ever, with no error. schedule_mail_sync asked
    get_transport(provider), which consults the DEPLOYMENT-WIDE registry and is empty when each
    organisation brings its own mailbox. Connecting a mailbox appeared to succeed and silently did
    nothing, which is the worst possible outcome for this feature: the operator would wait for mail
    that was never going to be fetched.
    """
    from agent import mail as mail_pkg
    from agent.mail.gateway import schedule_mail_sync, clear_transports

    clear_transports()  # prove it works with NO deployment-wide transport registered

    account = models.MailAccount(
        id=str(uuid.uuid4()),
        org_id=str(world['org'].id),
        agent_id=str(world['agent'].id),
        provider='imap',
        provider_account_id='ngo@example.org',
        connection_type=models.MailAccount.CONNECTION_CONNECTED_IMAP,
        address='ngo@example.org',
        status=models.MailAccount.ACTIVE,
        credentials_ref='mail/imap/probe',
        created_at=datetime.now(timezone.utc),
    )
    db.add(account)
    db.commit()

    queued = schedule_mail_sync(db, stale_seconds=60, org_id=str(world['org'].id))
    db.commit()

    assert queued == 1, (
        'a mailbox with its own credential must be queued even when no deployment-wide transport is '
        'registered; otherwise it never syncs and nothing says so'
    )
    workflow = db.query(models.AgentWorkflow).filter(
        models.AgentWorkflow.workflow_type == 'mail_sync'
    ).first()
    assert workflow is not None and workflow.subject_id == account.id


def test_a_mailbox_with_no_transport_and_no_credential_is_still_skipped(db, world):
    """The original behaviour, kept: dead work must not accumulate for a mailbox that could never be
    read. The exception is a mailbox that carries its own credential."""
    from agent.mail.gateway import schedule_mail_sync, clear_transports

    clear_transports()

    account = models.MailAccount(
        id=str(uuid.uuid4()),
        org_id=str(world['org'].id),
        agent_id=str(world['agent'].id),
        provider='imap',
        provider_account_id='bare@example.org',
        connection_type=models.MailAccount.CONNECTION_CONNECTED_IMAP,
        address='bare@example.org',
        status=models.MailAccount.ACTIVE,
        credentials_ref=None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(account)
    db.commit()

    assert schedule_mail_sync(db, stale_seconds=60, org_id=str(world['org'].id)) == 0


# ---------------------------------------------------------------------------
# The narrow definer path: the guard is the SQL, and the app must respect an empty answer
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeDB:
    def __init__(self, value):
        self._value = value
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        if self._value is _RAISE:
            raise RuntimeError('function does not exist')
        return _FakeResult(self._value)


_RAISE = object()


def _gateway(db, org_id):
    from agent.mail.gateway import MailGateway

    return MailGateway(db, org_id=org_id, agent_id='agent-1')


def _account(org_id, credentials_ref='mail/imap/x'):
    return models.MailAccount(
        id='account-1', org_id=org_id, agent_id='agent-1', provider='imap',
        provider_account_id='a@b.c', connection_type=models.MailAccount.CONNECTION_CONNECTED_IMAP,
        address='a@b.c', status=models.MailAccount.ACTIVE, credentials_ref=credentials_ref,
        created_at=datetime.now(timezone.utc),
    )


def test_an_empty_answer_from_the_definer_is_an_empty_answer():
    """The function returns nothing for an unbound caller and for an account in another
    organisation, and the app must treat those identically - it may not go looking for a reason."""
    store = object()
    gw = _gateway(_FakeDB(''), 'org-1')
    assert gw._payload_via_definer(_account('org-1'), store) is None


def test_a_missing_function_is_not_a_crash():
    gw = _gateway(_FakeDB(_RAISE), 'org-1')
    assert gw._payload_via_definer(_account('org-1'), object()) is None


def test_the_definer_passes_only_the_account_id():
    """It must not accept an organisation: the organisation comes from the caller's binding, and a
    function that took one could be asked on behalf of somebody else."""
    db = _FakeDB('')
    gw = _gateway(db, 'org-1')
    gw._payload_via_definer(_account('org-1'), object())
    statement, params = db.calls[0]
    assert params == {'account': 'account-1'}
    assert 'org' not in params


def test_ciphertext_from_the_definer_is_decrypted_by_the_store():
    from agent.credential_store import CredentialStore

    class _Store:
        def open_ciphertext(self, token):
            assert token == 'CIPHER'
            return {'host': 'imap.gmail.com', 'username': 'u', 'password': 'p'}

    gw = _gateway(_FakeDB('CIPHER'), 'org-1')
    payload = gw._payload_via_definer(_account('org-1'), _Store())
    assert payload['host'] == 'imap.gmail.com'


def test_an_undecryptable_ciphertext_is_not_a_crash():
    class _Store:
        def open_ciphertext(self, token):
            raise RuntimeError('InvalidToken')

    gw = _gateway(_FakeDB('CIPHER'), 'org-1')
    assert gw._payload_via_definer(_account('org-1'), _Store()) is None
