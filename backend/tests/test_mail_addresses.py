"""Allocating an NGO's permanent address: stable, unique, and never an operational one."""

import pytest

from agent.mail.addresses import DOMAIN, RESERVED, allocate, slugify


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,expected",
    [
        ("Globalmotion", "globalmotion"),
        ("War Child", "war-child"),
        # The legal form is stripped, so one organisation does not end up with two addresses.
        ("War Child Trust", "war-child"),
        ("War Child Trust Limited", "war-child"),
        ("St. Mary's Foundation", "st-marys"),
        ("African Development Bank Group", "african-development-bank"),
        ("  Spaced   Out  ", "spaced-out"),
        ("ACME (UK) Ltd", "acme-uk"),
    ],
)
def test_names_derive_a_clean_stable_local_part(name, expected):
    assert slugify(name) == expected


def test_derivation_is_stable():
    """The address is printed on grant applications; re-deriving it must not drift."""
    assert slugify("Globalmotion NGO") == slugify("Globalmotion NGO")


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------
def test_the_first_choice_is_the_bare_slug():
    out = allocate("Globalmotion")
    assert out.address == f"globalmotion@{DOMAIN}"


def test_a_collision_gets_a_readable_discriminator():
    out = allocate("Globalmotion", taken={"globalmotion"})
    assert out.local_part == "globalmotion-2"
    assert out.address.endswith(f"@{DOMAIN}")


def test_collisions_walk_until_free():
    out = allocate("Globalmotion", taken={"globalmotion", "globalmotion-2", "globalmotion-3"})
    assert out.local_part == "globalmotion-4"


def test_uniqueness_is_case_insensitive():
    out = allocate("Globalmotion", taken={"GLOBALMOTION"})
    assert out.local_part == "globalmotion-2"


def test_exhaustion_refuses_rather_than_looping():
    taken = {"globalmotion"} | {f"globalmotion-{i}" for i in range(2, 60)}
    with pytest.raises(ValueError):
        allocate("Globalmotion", taken=taken)


# ---------------------------------------------------------------------------
# Reserved words
# ---------------------------------------------------------------------------
def test_every_reserved_word_is_actually_reserved():
    for word in RESERVED:
        out = allocate(word)
        assert out.local_part not in RESERVED, f"{word} was handed out as an address"
        # Kept recognisable rather than replaced with something the NGO would not know.
        assert out.local_part.startswith(word) or word in out.local_part


def test_admin_is_never_assigned_to_an_ngo_named_admin():
    out = allocate("Admin")
    assert out.local_part != "admin"


def test_a_name_that_slugs_to_a_reserved_word_is_suffixed():
    # "Trust" strips to nothing, so the fallback must still produce something usable.
    out = allocate("Trust")
    assert out.local_part not in RESERVED
    assert len(out.local_part) >= 3


# ---------------------------------------------------------------------------
# Degenerate names
# ---------------------------------------------------------------------------
def test_a_name_with_no_usable_characters_still_yields_an_address():
    """The organisation is real even when its name is not Latin; refusing would block onboarding."""
    out = allocate("日本語")
    assert out.address.endswith(f"@{DOMAIN}")
    assert len(out.local_part) >= 3
    assert out.local_part not in RESERVED


def test_a_very_short_name_is_extended():
    out = allocate("AB")
    assert len(out.local_part) >= 3


def test_a_very_long_name_is_bounded():
    out = allocate("A" * 200)
    assert len(out.local_part) <= 48


def test_an_empty_name_does_not_crash():
    out = allocate("")
    assert out.address.endswith(f"@{DOMAIN}")


# ---------------------------------------------------------------------------
# Portal mailboxes - unpredictable on purpose
# ---------------------------------------------------------------------------
def test_a_portal_mailbox_is_not_guessable():
    """A mailbox receiving verification codes and recovery links must not be derivable from the
    organisation's name: anyone who guessed it could trigger password resets against its accounts."""
    from agent.mail.addresses import allocate_mailbox

    a = allocate_mailbox("Globalmotion")
    assert a.address != allocate("Globalmotion").address, "the friendly slug is guessable"
    assert a.local_part.startswith("globalmotion-"), "the org must stay identifiable in a listing"
    assert len(a.local_part.split("-")[-1]) >= 8, "the suffix carries the entropy"


def test_two_mailboxes_for_one_organisation_differ():
    from agent.mail.addresses import allocate_mailbox

    seen = {allocate_mailbox("Globalmotion").local_part for _ in range(20)}
    assert len(seen) == 20, "a collision would hand one portal another portal's codes"


def test_a_portal_mailbox_avoids_a_taken_suffix():
    from agent.mail.addresses import allocate_mailbox

    first = allocate_mailbox("Globalmotion")
    second = allocate_mailbox("Globalmotion", taken={first.local_part})
    assert second.local_part != first.local_part


def test_a_portal_mailbox_is_bounded_and_reserved_safe():
    from agent.mail.addresses import allocate_mailbox

    assert len(allocate_mailbox("A" * 200).local_part) <= 48
    for word in ("admin", "postmaster", "hostmaster"):
        assert allocate_mailbox(word).local_part not in RESERVED
