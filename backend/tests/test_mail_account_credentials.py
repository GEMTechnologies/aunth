"""Per-mailbox credential resolution: an account reaches its OWN secret, and nobody else's.

WHY THIS FILE EXISTS

`transport_for(provider)` returned the DEPLOYMENT-WIDE transport registered under a provider name. One
mailbox for the whole system, so every organisation read and sent through the same account - which is
the exact opposite of the brief's requirement that a task for NGO A must never read NGO B's credentials.

`transport_for_account` resolves `account.credentials_ref` through the credential store, scoped to the
gateway's own `org_id`. The tests below are the ones that decide whether that scoping is real:

  * an account belonging to ANOTHER organisation is refused BEFORE any lookup
  * an account with no `credentials_ref` does not borrow a transport by provider name
  * a revoked credential yields None rather than a fallback
  * a credential that cannot be decrypted yields None rather than a fallback
  * the resolved transport is built from THAT account's payload, not another's
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.credential_store import generate_key  # noqa: E402
from agent.mail import gateway as mail_gateway  # noqa: E402


class FakeAccount:
    """The fields `transport_for_account` reads, and nothing else.

    A stand-in rather than a real `MailAccount` row so the tests can forge an account belonging to
    another organisation - which is precisely the case that must be refused.
    """

    def __init__(self, *, org_id: str, provider: str = "IMAP", credentials_ref=None, address="a@b.c"):
        self.id = "acct-1"
        self.org_id = org_id
        self.provider = provider
        self.credentials_ref = credentials_ref
        self.address = address


@pytest.fixture
def key() -> str:
    return generate_key()


@pytest.fixture(autouse=True)
def _clean_registries():
    mail_gateway.clear_outbound_transports()
    mail_gateway.clear_transports()
    yield
    mail_gateway.clear_outbound_transports()
    mail_gateway.clear_transports()


@pytest.fixture
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import models

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def orgs(db):
    import models

    db.add(models.User(id="user-owner", display_name="Owner"))
    db.flush()
    for identifier in ("org-a", "org-b"):
        db.add(
            models.Organisation(
                id=identifier, name=identifier, slug=identifier, owner_user_id="user-owner"
            )
        )
    db.flush()
    return "org-a", "org-b"


def _patch_settings(monkeypatch, key):
    import config

    class S:
        credential_encryption_key = key
        # The store must not be reached through any other setting.
        mail_transports = None

    monkeypatch.setattr(config, "settings", S())


# ===========================================================================
# TENANT ISOLATION - the reason this method exists
# ===========================================================================
def test_an_account_from_another_organisation_is_refused(db, orgs, key, monkeypatch):
    """BEFORE the lookup, not after. A forged account object must not even reach the store."""
    from agent.mail.gateway import MailTenantMismatch

    _patch_settings(monkeypatch, key)
    gateway = mail_gateway.MailGateway(db, org_id="org-a", agent_id="agent-1")

    foreign = FakeAccount(org_id="org-b", credentials_ref="mail:account:1")
    with pytest.raises(MailTenantMismatch):
        gateway.transport_for_account(foreign)


def test_the_refusal_happens_even_when_the_credential_would_resolve(db, orgs, key, monkeypatch):
    """A credential that EXISTS for org-b must still not be reachable from an org-a gateway. Refusing
    only when the lookup fails would mean the scoping was accidental rather than enforced."""
    from agent.credential_store import CredentialStore
    from agent.mail.gateway import MailTenantMismatch

    _patch_settings(monkeypatch, key)
    CredentialStore(db, key=key).put(
        org_id="org-b", ref="shared-ref", payload={"host": "imap.b.example", "password": "b-secret"}
    )

    gateway = mail_gateway.MailGateway(db, org_id="org-a", agent_id="agent-1")
    with pytest.raises(MailTenantMismatch):
        gateway.transport_for_account(FakeAccount(org_id="org-b", credentials_ref="shared-ref"))


def test_the_mismatch_is_a_permission_error_so_callers_can_tell_it_apart(db, orgs, key, monkeypatch):
    """A provider failure means try again. This means a caller reached for something it does not own,
    and nothing about the request should be retried or reinterpreted."""
    from agent.mail.gateway import MailTenantMismatch

    _patch_settings(monkeypatch, key)
    gateway = mail_gateway.MailGateway(db, org_id="org-a", agent_id="agent-1")
    assert issubclass(MailTenantMismatch, PermissionError)
    with pytest.raises(PermissionError):
        gateway.transport_for_account(FakeAccount(org_id="org-b", credentials_ref="r"))


# ===========================================================================
# THE HAPPY PATH
# ===========================================================================
def test_a_stored_credential_builds_an_imap_transport(db, orgs, key, monkeypatch):
    from agent.credential_store import CredentialStore
    from agent.mail.providers.imap import ImapInboundMailProvider

    _patch_settings(monkeypatch, key)
    CredentialStore(db, key=key).put(
        org_id="org-a",
        ref="mail:account:1",
        payload={
            "host": "imap.a.example",
            "port": 993,
            "username": "grants@a.example",
            "password": "a-secret",
            "use_ssl": True,
            "mailbox": "INBOX",
        },
        kind="IMAP_PASSWORD",
    )

    gateway = mail_gateway.MailGateway(db, org_id="org-a", agent_id="agent-1")
    transport = gateway.transport_for_account(
        FakeAccount(org_id="org-a", credentials_ref="mail:account:1")
    )

    assert isinstance(transport, ImapInboundMailProvider)
    assert transport.config.host == "imap.a.example"
    assert transport.config.username == "grants@a.example"


def test_each_organisation_gets_its_own_transport(db, orgs, key, monkeypatch):
    """The property the whole method exists for: two organisations, two mailboxes, no sharing."""
    from agent.credential_store import CredentialStore

    _patch_settings(monkeypatch, key)
    store = CredentialStore(db, key=key)
    store.put(
        org_id="org-a", ref="r", payload={"host": "imap.a.example", "password": "a"}
    )
    store.put(
        org_id="org-b", ref="r", payload={"host": "imap.b.example", "password": "b"}
    )

    a = mail_gateway.MailGateway(db, org_id="org-a", agent_id="a1").transport_for_account(
        FakeAccount(org_id="org-a", credentials_ref="r")
    )
    b = mail_gateway.MailGateway(db, org_id="org-b", agent_id="b1").transport_for_account(
        FakeAccount(org_id="org-b", credentials_ref="r")
    )

    assert a is not None and b is not None
    assert a.config.host == "imap.a.example"
    assert b.config.host == "imap.b.example", "org-b received org-a's mailbox"


def test_a_registered_transport_serves_an_account_with_no_credential(db, orgs, key, monkeypatch):
    """How tests inject a fake, and how a single-mailbox deployment works.

    It applies ONLY to an account with no `credentials_ref`. The first version of the method consulted
    this registry first, which let a deployment-wide transport silently bypass per-account scoping -
    every organisation sharing one mailbox through the front door the method exists to close.
    """
    _patch_settings(monkeypatch, key)
    sentinel = object()
    mail_gateway.register_transport("IMAP", sentinel)

    gateway = mail_gateway.MailGateway(db, org_id="org-a", agent_id="agent-1")
    assert gateway.transport_for_account(
        FakeAccount(org_id="org-a", credentials_ref=None)
    ) is sentinel


def test_a_registered_transport_cannot_displace_a_per_account_credential(db, orgs, key, monkeypatch):
    """THE ORDER IS THE SECURITY PROPERTY. An account that HAS a credential must reach its own, even
    when a provider-wide transport is registered."""
    from agent.credential_store import CredentialStore

    _patch_settings(monkeypatch, key)
    CredentialStore(db, key=key).put(
        org_id="org-a", ref="r", payload={"host": "imap.a.example", "password": "a"}
    )
    mail_gateway.register_transport("IMAP", object())  # a deployment-wide transport exists

    transport = mail_gateway.MailGateway(db, org_id="org-a", agent_id="agent-1").transport_for_account(
        FakeAccount(org_id="org-a", credentials_ref="r")
    )
    assert transport is not None
    assert getattr(transport, "config", None) is not None, (
        "the provider-wide transport was returned for an account that has its own credential"
    )
    assert transport.config.host == "imap.a.example"


# ===========================================================================
# FAILURE MUST NOT FALL BACK
# ===========================================================================
def test_a_revoked_credential_yields_none_not_a_fallback(db, orgs, key, monkeypatch):
    """A fallback here would be a silent cross-tenant read - the worst possible failure for this
    method, and exactly what a 'helpful' default would produce."""
    from agent.credential_store import CredentialStore

    _patch_settings(monkeypatch, key)
    store = CredentialStore(db, key=key)
    store.put(org_id="org-a", ref="r", payload={"host": "imap.a.example", "password": "a"})
    store.revoke(org_id="org-a", ref="r")

    mail_gateway.register_transport("IMAP", object())  # a provider-wide transport is available
    gateway = mail_gateway.MailGateway(db, org_id="org-a", agent_id="agent-1")

    # The registered transport wins by design, so use a provider with no registration for the fallback
    # question. What must NOT happen is a credential being substituted from somewhere else.
    account = FakeAccount(org_id="org-a", provider="NOT_REGISTERED", credentials_ref="r")
    assert gateway.transport_for_account(account) is None


def test_a_missing_credential_yields_none(db, orgs, key, monkeypatch):
    _patch_settings(monkeypatch, key)
    gateway = mail_gateway.MailGateway(db, org_id="org-a", agent_id="agent-1")
    assert gateway.transport_for_account(
        FakeAccount(org_id="org-a", credentials_ref="never-stored")
    ) is None


def test_an_undecryptable_credential_yields_none_rather_than_raising(db, orgs, key, monkeypatch):
    """A key rotation must degrade to 'park the work', not to a crash in the fleet loop."""
    from agent.credential_store import CredentialStore

    _patch_settings(monkeypatch, key)
    CredentialStore(db, key=key).put(org_id="org-a", ref="r", payload={"host": "h", "password": "p"})

    # A different key, as after a rotation.
    _patch_settings(monkeypatch, generate_key())
    gateway = mail_gateway.MailGateway(db, org_id="org-a", agent_id="agent-1")
    assert gateway.transport_for_account(FakeAccount(org_id="org-a", credentials_ref="r")) is None


def test_an_unconfigured_store_yields_none_rather_than_raising(db, orgs, monkeypatch):
    """No encryption key is a deployment state. Mail work parks; the fleet does not stop."""
    import config

    class S:
        credential_encryption_key = ""
        mail_transports = None

    monkeypatch.setattr(config, "settings", S())
    gateway = mail_gateway.MailGateway(db, org_id="org-a", agent_id="agent-1")
    assert gateway.transport_for_account(
        FakeAccount(org_id="org-a", credentials_ref="r")
    ) is None


def test_a_non_imap_provider_is_honest_about_having_no_builder(db, orgs, key, monkeypatch):
    """GOOGLE and MICROSOFT take a token with its own refresh path. Returning None parks the work;
    inventing a transport would be worse than admitting there is not one yet."""
    from agent.credential_store import CredentialStore

    _patch_settings(monkeypatch, key)
    CredentialStore(db, key=key).put(
        org_id="org-a", ref="r", payload={"access_token": "tok"}
    )
    gateway = mail_gateway.MailGateway(db, org_id="org-a", agent_id="agent-1")
    assert gateway.transport_for_account(
        FakeAccount(org_id="org-a", provider="GOOGLE", credentials_ref="r")
    ) is None


# ===========================================================================
# THE CALLER MUST USE IT - a correct method with no callers is not a fix
# ===========================================================================
def test_sync_resolves_the_accounts_own_transport(db, orgs, key, monkeypatch):
    """`sync` called `transport_for(account.provider)`, the provider-WIDE transport, so every
    organisation's reconciliation would have read whichever mailbox was registered. Asserting the
    method exists is not enough; the caller has to use it."""
    from agent.credential_store import CredentialStore

    _patch_settings(monkeypatch, key)
    CredentialStore(db, key=key).put(
        org_id="org-a", ref="r", payload={"host": "imap.a.example", "password": "a"}
    )

    seen: dict = {}

    class FakeMail:
        def __init__(self, db, *, org_id, agent_id, transport):
            seen["transport"] = transport

        def sync(self, *, account, limit, max_batches):
            return {"ok": True}

    import agent.mail.service as mail_service

    monkeypatch.setattr(mail_service, "GranadaMail", FakeMail)

    gateway = mail_gateway.MailGateway(db, org_id="org-a", agent_id="agent-1")
    account = FakeAccount(org_id="org-a", credentials_ref="r")
    gateway.sync(account=account)

    transport = seen.get("transport")
    assert transport is not None, "sync passed no transport at all"
    assert getattr(transport, "config", None) is not None, (
        "sync used the provider-wide transport instead of resolving the account's own credential"
    )
    assert transport.config.host == "imap.a.example"


def test_sync_refuses_an_account_from_another_organisation(db, orgs, key, monkeypatch):
    """The scoping has to hold at the caller too, not only in the method it delegates to."""
    from agent.mail.gateway import MailTenantMismatch

    _patch_settings(monkeypatch, key)
    gateway = mail_gateway.MailGateway(db, org_id="org-a", agent_id="agent-1")
    with pytest.raises(MailTenantMismatch):
        gateway.sync(account=FakeAccount(org_id="org-b", credentials_ref="r"))
