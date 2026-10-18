"""Which pages count as "the organisation's own site".

THE BUG THIS PINS. Matching exact hosts meant a mirror or CDN host was rejected as somebody else's site.
Measured on a real organisation: its search results came back on `origin4-www.<domain>` and every one was
filtered out, so the multi-page reading silently degraded to the front page - the feature was present,
correct, and did nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.website_intake import (  # noqa: E402
    _candidate_links,
    _registrable_domain,
    _same_site,
)


def test_a_cdn_or_www_mirror_is_still_the_same_site():
    assert _same_site("https://origin4-www.unicef.org/media/x", "https://www.unicef.org")
    assert _same_site("https://unicef.org/about", "https://www.unicef.org")
    assert _same_site("https://www.unicef.org", "https://unicef.org")


def test_an_unrelated_organisation_is_not_the_same_site():
    assert not _same_site("https://other.org/about", "https://www.unicef.org")
    assert not _same_site("https://unicef.org.evil.com/x", "https://www.unicef.org")


def test_two_different_organisations_under_a_public_suffix_are_not_the_same_site():
    """`a.co.uk` and `b.co.uk` share a suffix and nothing else.

    A naive "last two labels" rule would call both `co.uk` and merge two unrelated organisations - which
    is the unsafe direction, since it would attribute one body's pages to another.
    """
    assert _registrable_domain("a.example.co.uk") == "example.co.uk"
    assert not _same_site("https://a.co.uk/x", "https://b.co.uk/y")
    assert _same_site("https://a.example.co.uk", "https://b.example.co.uk")


def test_link_discovery_prefers_about_and_contact_pages():
    markdown = (
        "Read [About us](https://ngo.org/about) or [Contact](https://ngo.org/contact-us). "
        "Also [News](https://ngo.org/news) and [a partner](https://partner.org/about)."
    )
    links = _candidate_links(markdown, "https://ngo.org")
    assert "https://ngo.org/about" in links
    assert "https://ngo.org/contact-us" in links
    assert all("news" not in link for link in links)
    assert all("partner.org" not in link for link in links)


def test_link_discovery_follows_a_mirror_link_but_not_a_stranger():
    markdown = (
        "[Who we are](https://www.ngo.org/who-we-are) [elsewhere](https://unrelated.com/about)"
    )
    links = _candidate_links(markdown, "https://ngo.org")
    assert links == ["https://www.ngo.org/who-we-are"]
