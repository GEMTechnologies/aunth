"""Portal accounts: generated once, stored encrypted, read back forever.

The invariant under test is that a password is NEVER regenerated. A second password for one account
means the first - the only one the portal knows - exists nowhere, and the account is lost.
"""

import pytest

from agent.portal_accounts import (
    PASSWORD_FIELD_NAMES,
    account_ref,
    ensure_account,
    generate_password,
)


class FakeStore:
    """Stands in for the encrypted credential store.

    Raises on a missing ref exactly as `CredentialStore.get` does - returning None would let a broken
    implementation pass, since the real one's absence arrives as an exception.
    """

    def __init__(self):
        self.rows = {}
        self.puts = 0

    def get(self, *, org_id, ref):
        key = (org_id, ref)
        if key not in self.rows:
            raise KeyError(f"no credential for {ref!r}")
        return dict(self.rows[key])

    def put(self, *, org_id, ref, payload, kind=None, note=None):
        self.puts += 1
        self.rows[(org_id, ref)] = dict(payload)
        return None


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def test_a_password_has_every_required_class():
    for _ in range(20):
        pw = generate_password()
        assert any(c.islower() for c in pw)
        assert any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert any(c in "!@#$%^&*-_=+" for c in pw)


def test_ambiguous_glyphs_are_excluded():
    """A password nobody can transcribe is a support burden, and this one may be read off a screen."""
    for _ in range(20):
        pw = generate_password()
        assert not (set(pw) & set("O0oIl1|`'\"\\"))


def test_passwords_are_not_repeated():
    assert len({generate_password() for _ in range(50)}) == 50


def test_a_short_password_is_refused():
    with pytest.raises(ValueError):
        generate_password(8)


def test_the_default_length_is_strong():
    assert len(generate_password()) >= 16


# ---------------------------------------------------------------------------
# Idempotency - the whole point
# ---------------------------------------------------------------------------
def test_the_first_call_creates_and_stores():
    store = FakeStore()
    acct = ensure_account(store, org_id="org-1", portal_host="unpartnerportal.org",
                          email="globalmotion@granadac.com")
    assert acct.created_now is True
    assert acct.email == "globalmotion@granadac.com"
    assert store.puts == 1
    assert store.rows[("org-1", "portal-account:unpartnerportal.org")]["password"] == acct.password


def test_a_second_call_returns_the_SAME_password():
    """If this ever fails, the registration account is unreachable - the portal only knows the first
    password, and it would now exist nowhere."""
    store = FakeStore()
    first = ensure_account(store, org_id="org-1", portal_host="unpartnerportal.org",
                           email="globalmotion@granadac.com")
    second = ensure_account(store, org_id="org-1", portal_host="unpartnerportal.org",
                            email="globalmotion@granadac.com")
    assert second.password == first.password
    assert second.created_now is False
    assert store.puts == 1, "a second password must never be written"


def test_the_stored_email_is_the_authority_not_the_argument():
    """The credential is self-describing: a password with no idea which address it belongs to cannot
    be used by anyone."""
    store = FakeStore()
    ensure_account(store, org_id="org-1", portal_host="unpartnerportal.org",
                   email="first@granadac.com")
    again = ensure_account(store, org_id="org-1", portal_host="unpartnerportal.org",
                           email="someone-else@granadac.com")
    assert again.email == "first@granadac.com"


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------
def test_two_organisations_do_not_share_an_account():
    store = FakeStore()
    a = ensure_account(store, org_id="org-1", portal_host="unpartnerportal.org", email="a@granadac.com")
    b = ensure_account(store, org_id="org-2", portal_host="unpartnerportal.org", email="b@granadac.com")
    assert a.password != b.password


def test_two_portals_get_two_accounts():
    store = FakeStore()
    a = ensure_account(store, org_id="org-1", portal_host="unpartnerportal.org", email="a@granadac.com")
    b = ensure_account(store, org_id="org-1", portal_host="ungm.org", email="a@granadac.com")
    assert a.password != b.password


def test_an_account_needs_an_organisation_and_an_address():
    store = FakeStore()
    with pytest.raises(ValueError):
        ensure_account(store, org_id="", portal_host="x.org", email="a@granadac.com")
    with pytest.raises(ValueError):
        ensure_account(store, org_id="org-1", portal_host="x.org", email="")


def test_a_ref_needs_a_host():
    with pytest.raises(ValueError):
        account_ref("")


def test_the_password_is_offered_under_the_names_portals_use():
    store = FakeStore()
    acct = ensure_account(store, org_id="org-1", portal_host="unpartnerportal.org",
                          email="a@granadac.com")
    values = acct.values()
    assert values["password"] == acct.password
    assert values["UN_PASSWORD_1"] == acct.password
    assert set(values) == set(PASSWORD_FIELD_NAMES)
