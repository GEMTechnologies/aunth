"""The producer half of the funding catalogue: source mapping, bounding, and failure isolation.

WHAT THESE TESTS PROTECT. Two failures matter more than any other here, and both were measured
against the live API rather than imagined:

1. **A long descriptive field aborting the whole sweep.** grants.gov returns a 108-character funding
   category against a `varchar(100)` column, and the resulting `StringDataRightTruncation` aborted
   every other opportunity in the batch. A sweep that dies on field width is a bot that works in a
   test and not in production.
2. **A URL stored as an email address.** Some records put `http://www.eda.gov/contact/` in the
   contact-email field. Stored as-is, every downstream consumer treats a web form as an address.

Every test injects an opener, so none of them touch the network: the mapping is the thing under test,
and a suite that calls a live funding API would fail on someone else's outage.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.opportunity_ingestion import OpportunityIngestionAdapter, RawOpportunity
from agent.opportunity_sources import (
    MAX_PER_SWEEP,
    DiscoveryError,
    GrantsGovSource,
    _looks_like_email,
    _parse_us_date,
    _strip_html,
    build_sources,
    discover,
)


def search_body(hits):
    return {"errorcode": 0, "msg": "Webservice Succeeds", "data": {"oppHits": hits}}


def hit(**overrides):
    base = {
        "id": 334326,
        "number": "21-595",
        "title": "Tribal Colleges and Universities Program (TCUP)",
        "agency": "U.S. National Science Foundation",
        "agencyCode": "NSF",
        "openDate": "06/24/2021",
        "closeDate": "01/11/2027",
        "oppStatus": "posted",
        "docType": "synopsis",
        "cfdaList": ["47.076"],
    }
    base.update(overrides)
    return base


def detail_body(**synopsis_overrides):
    synopsis = {
        "agencyName": "U.S. National Science Foundation",
        "synopsisDesc": "<p>Provides awards to <b>Tribal</b> Colleges.</p>",
        "responseDate": "Jan 11, 2027 12:00:00 AM EST",
        "awardCeiling": 3500000,
        "awardFloor": 100000,
        "applicantEligibilityDesc": "Proposals may only be submitted by Tribal Colleges.",
        "agencyContactEmail": "tcup@nsf.gov",
        "agencyContactPhone": "703-292-5111",
        "fundingActivityCategories": [{"description": "Science and Technology"}],
        "applicantTypes": [{"description": "Public and State controlled institutions"}],
    }
    synopsis.update(synopsis_overrides)
    return {"errorcode": 0, "data": {"id": 334326, "synopsis": synopsis}}


class FakeOpener:
    """Answers the two endpoints with fixtures, and records what was asked."""

    def __init__(self, *, search=None, detail=None, fail_detail=False, fail_search=False):
        self.search = search_body(search if search is not None else [hit()])
        self.detail = detail if detail is not None else detail_body()
        self.fail_detail = fail_detail
        self.fail_search = fail_search
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, body):
        self.calls.append((url, body))
        if "search2" in url:
            if self.fail_search:
                raise DiscoveryError("search is down")
            return self.search
        if self.fail_detail:
            raise DiscoveryError("detail is down")
        return self.detail


# ---------------------------------------------------------------------------
# Mapping a real source payload onto the ingestion contract
# ---------------------------------------------------------------------------
def test_a_live_shaped_hit_becomes_a_valid_delivery():
    source = GrantsGovSource(opener=FakeOpener())
    result = source.fetch(limit=5, with_detail=True)

    assert result.errors == []
    assert len(result.opportunities) == 1
    raw = result.opportunities[0]
    assert raw.title == "Tribal Colleges and Universities Program (TCUP)"
    assert raw.source_url == "https://www.grants.gov/search-results-detail/334326"
    assert raw.source_name == "Grants.gov"
    assert raw.country == "United States"
    assert raw.source_id == "334326"
    assert raw.deadline == datetime(2027, 1, 11, tzinfo=timezone.utc)
    assert (raw.amount_min, raw.amount_max) == (100000, 3500000)
    assert raw.currency == "USD"
    assert raw.contact_email == "tcup@nsf.gov"
    assert raw.is_active is True

    # The whole point: the adapter accepts it without a second schema.
    OpportunityIngestionAdapter.validate(raw)


def test_the_content_hash_is_stable_for_the_same_notice():
    """A re-listing, a corrected title or an extended deadline is an update of the same programme -
    so the producer's identity must come from the source's own identifier, not the volatile fields.

    Exercised through `_to_raw` rather than `fetch`, because `fetch` already collapses two hits that
    carry the same source id: that is a separate guarantee, tested below.
    """
    source = GrantsGovSource(opener=FakeOpener())
    first = source._to_raw(hit(), None)
    second = source._to_raw(
        hit(title="Tribal Colleges and Universities Program (TCUP) - REVISED", closeDate="03/01/2027"),
        None,
    )
    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 64


def test_the_same_notice_is_fetched_once_per_sweep():
    """The source's own id is the identity within a sweep, so two keywords that both match one
    programme must not produce two deliveries of it."""
    same_id = [hit(), hit(title="Same programme, listed twice")]
    source = GrantsGovSource(opener=FakeOpener(search=same_id))
    assert len(source.fetch(limit=5, with_detail=False).opportunities) == 1


def test_two_different_notices_get_different_hashes():
    two = [hit(), hit(id=999999, number="99-001", title="A different programme")]
    source = GrantsGovSource(opener=FakeOpener(search=two))
    a, b = source.fetch(limit=5, with_detail=False).opportunities
    assert a.content_hash != b.content_hash
    assert a.source_url != b.source_url


def test_a_long_funding_category_does_not_break_the_delivery():
    """MEASURED LIVE. The category sentence is 108 characters against a varchar(100); stored
    unbounded it aborted the entire batch with StringDataRightTruncation."""
    long_category = "Other (see text field entitled \"Explanation of Other Category of Funding Activity\" for clarification)"
    assert len(long_category) > 100
    source = GrantsGovSource(
        opener=FakeOpener(detail=detail_body(fundingActivityCategories=[{"description": long_category}]))
    )
    raw = source.fetch(limit=5).opportunities[0]
    assert raw.sector is not None
    assert len(raw.sector) <= 100
    OpportunityIngestionAdapter.validate(raw)


def test_a_url_in_the_contact_email_field_is_not_stored_as_an_email():
    """MEASURED LIVE. grants.gov returns `http://www.eda.gov/contact/` for some records."""
    source = GrantsGovSource(opener=FakeOpener(detail=detail_body(agencyContactEmail="http://www.eda.gov/contact/")))
    raw = source.fetch(limit=5).opportunities[0]
    assert raw.contact_email is None


def test_a_contradictory_award_range_keeps_the_ceiling():
    """The adapter rejects floor > ceiling outright, which would cost the whole opportunity. The
    ceiling is the number a reader acts on, so it survives and the bad floor is dropped."""
    source = GrantsGovSource(opener=FakeOpener(detail=detail_body(awardFloor=9000000, awardCeiling=1000)))
    raw = source.fetch(limit=5).opportunities[0]
    assert raw.amount_max == 1000
    assert raw.amount_min is None
    OpportunityIngestionAdapter.validate(raw)


def test_a_hit_without_a_title_is_dropped_rather_than_guessed():
    source = GrantsGovSource(opener=FakeOpener(search=[hit(title=""), hit(id=222, title="")]))
    result = source.fetch(limit=5, with_detail=False)
    assert result.opportunities == []
    assert len(result.errors) == 2


def test_a_failed_detail_call_costs_the_detail_not_the_opportunity():
    source = GrantsGovSource(opener=FakeOpener(fail_detail=True))
    result = source.fetch(limit=5, with_detail=True)
    assert len(result.opportunities) == 1, "the listing is still worth storing"
    assert result.errors, "the detail failure must be visible, not silent"
    assert result.opportunities[0].description is None


def test_a_failed_search_is_reported_and_yields_nothing():
    source = GrantsGovSource(opener=FakeOpener(fail_search=True))
    result = source.fetch(limit=5)
    assert result.opportunities == []
    assert result.errors and "down" in result.errors[0]
    assert result.ok is False


def test_a_source_error_code_is_not_treated_as_success():
    opener = FakeOpener()
    opener.search = {"errorcode": 1, "msg": "invalid keyword", "data": {}}
    result = GrantsGovSource(opener=opener).fetch(limit=5)
    assert result.opportunities == []
    assert result.ok is False


def test_duplicate_hits_across_keywords_are_fetched_once():
    opener = FakeOpener(search=[hit(), hit()])
    source = GrantsGovSource(keywords=["a", "b", "c"], opener=opener)
    result = source.fetch(limit=9, with_detail=False)
    assert len(result.opportunities) == 1


def test_the_sweep_is_bounded():
    opener = FakeOpener(search=[hit(id=i, source_url=None) for i in range(1, 40)])
    source = GrantsGovSource(opener=opener)
    result = source.fetch(limit=MAX_PER_SWEEP * 5, with_detail=False)
    assert len(result.opportunities) <= MAX_PER_SWEEP


def test_detail_fetching_is_capped_independently_of_the_page_size():
    """A large page must not become a hundred sequential requests against one endpoint."""
    opener = FakeOpener(search=[hit(id=i) for i in range(1, 25)])
    GrantsGovSource(opener=opener).fetch(limit=24, with_detail=True, with_detail_limit=3)
    detail_calls = [c for c in opener.calls if "fetchOpportunity" in c[0]]
    assert len(detail_calls) == 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("01/11/2027", datetime(2027, 1, 11, tzinfo=timezone.utc)),
        ("Jan 11, 2027 12:00:00 AM EST", datetime(2027, 1, 11, tzinfo=timezone.utc)),
        ("2027-01-11", datetime(2027, 1, 11, tzinfo=timezone.utc)),
        ("", None),
        (None, None),
        ("not a date", None),
    ],
)
def test_dates_parse_or_are_absent_never_invented(value, expected):
    assert _parse_us_date(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("tcup@nsf.gov", True),
        ("http://www.eda.gov/contact/", False),
        ("https://example.org/form", False),
        ("no-at-sign", False),
        ("@example.org", False),
        ("a@b", False),
        (None, False),
    ],
)
def test_only_real_addresses_are_stored_as_emails(value, expected):
    assert _looks_like_email(value) is expected


def test_html_is_flattened_for_a_reader():
    assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"
    assert _strip_html("a<br/>b") == "a\nb"
    assert _strip_html("&amp; &lt;x&gt;") == "& <x>"
    assert _strip_html(None) is None
    assert _strip_html("   ") is None


# ---------------------------------------------------------------------------
# Failure isolation across sources
# ---------------------------------------------------------------------------
class ExplodingSource:
    NAME = "Exploding"

    def fetch(self, **kwargs):
        raise RuntimeError("boom")


class WorkingSource:
    NAME = "Working"

    def fetch(self, **kwargs):
        from agent.opportunity_sources import SourceFetch

        return SourceFetch(source_name=self.NAME, opportunities=[])


def test_one_dead_source_does_not_stop_the_others():
    results = discover([ExplodingSource(), WorkingSource()], limit=1)
    assert len(results) == 2
    assert results[0].errors and "boom" in results[0].errors[0]
    assert results[1].ok


def test_an_unknown_source_name_is_skipped_not_raised():
    assert build_sources(["no_such_source", "grants_gov"]) != []
    assert len(build_sources(["no_such_source"])) == 0


def test_the_registry_contains_the_working_source():
    assert build_sources(["grants_gov"], keywords=["x"])[0].NAME == "Grants.gov"
