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
