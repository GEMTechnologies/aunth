"""SMTP becomes reachable without a start-up hook.

WHY THIS FILE EXISTS

Every mail adapter was reachable from a test and from nowhere else. `register_outbound_transport` and
`register_transport` appear in exactly three places in the repository, and all three are test files -
so a deployment could set `smtp_host`, restart, and still have `get_outbound_transport("SMTP")` return
None. Configuration that is accepted and ignored is worse than configuration that is refused.

These tests assert the CONFIGURATION PATH, not the adapter: that setting a host produces a transport,
that not setting one produces None rather than an exception, and that the answer is cached.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.mail import gateway as mail_gateway  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registries():
    mail_gateway.clear_outbound_transports()
    yield
    mail_gateway.clear_outbound_transports()


def _patch_settings(monkeypatch, **values):
    """Point `config.settings` at a stand-in with the given attributes.

    Patched on the `config` module because `get_outbound_transport` imports it inside the function -
    which is deliberate, so the scheduler does not pull the mail stack in at import time.
    """
    import config

    class StandIn:
        pass

    stand_in = StandIn()
    for key, value in values.items():
        setattr(stand_in, key, value)
    monkeypatch.setattr(config, "settings", stand_in)
    return stand_in


def test_an_unconfigured_smtp_transport_is_none_not_an_exception(monkeypatch):
    """`None` is a deployment state an operator fixes. An exception looks like a bug and invites a
    workaround, which is how a mailbox ends up sending through something nobody audited."""
    _patch_settings(monkeypatch, smtp_host="localhost")
    assert mail_gateway.get_outbound_transport("SMTP") is None


def test_a_configured_smtp_host_produces_a_transport(monkeypatch):
    _patch_settings(
        monkeypatch,
        smtp_host="smtp.example.org",
        smtp_port=587,
        smtp_user="u",
        smtp_pass="p",
        smtp_tls=True,
        smtp_ssl=False,
    )
    transport = mail_gateway.get_outbound_transport("SMTP")
    assert transport is not None
    assert transport.name == "SMTP"
    assert "MAIL_SEND" in transport.capabilities


def test_the_built_transport_declares_only_send_capabilities(monkeypatch):
    """A read scope must not imply a send scope. SMTP can send and cannot read, so it must not
    advertise anything that would let the authority check treat it as a mailbox."""
    _patch_settings(
        monkeypatch,
        smtp_host="smtp.example.org",
        smtp_port=587,
        smtp_user="",
        smtp_pass="",
        smtp_tls=True,
        smtp_ssl=False,
    )
    capabilities = mail_gateway.get_outbound_transport("SMTP").capabilities
    assert "MAIL_SEND" in capabilities
    assert "MAIL_READ" not in capabilities
    assert "MAIL_RECONCILE_SEND" not in capabilities, (
        "SMTP cannot reconcile, and advertising it would let the pipeline ask a question this "
        "adapter is guaranteed to answer with 'unknown'"
    )


def test_the_built_transport_is_cached(monkeypatch):
    """Constructed once per process, not once per message."""
    _patch_settings(
        monkeypatch,
        smtp_host="smtp.example.org",
        smtp_port=587,
        smtp_user="",
        smtp_pass="",
        smtp_tls=True,
        smtp_ssl=False,
    )
    first = mail_gateway.get_outbound_transport("SMTP")
    second = mail_gateway.get_outbound_transport("SMTP")
    assert first is second


def test_a_non_smtp_provider_is_unaffected(monkeypatch):
    """The SMTP branch must not invent a transport for a provider that has none - that would make
    every unconfigured mailbox look send-capable."""
    _patch_settings(monkeypatch, smtp_host="smtp.example.org")
    assert mail_gateway.get_outbound_transport("GOOGLE") is None
    assert mail_gateway.get_outbound_transport("") is None


def test_a_settings_provided_transport_still_wins(monkeypatch):
    """An operator who wires a transport object explicitly must not be overridden by the factory."""
    sentinel = object()
    _patch_settings(
        monkeypatch,
        smtp_host="smtp.example.org",
        outbound_mail_transports={"SMTP": sentinel},
    )
    assert mail_gateway.get_outbound_transport("SMTP") is sentinel
