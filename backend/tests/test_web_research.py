"""Web search and fetch: the parsing, the failures, and the provenance.

These are the properties that matter. Anything built on this module - a research note, a document, a
filing - cites what it read, so a hit without a URL is worse than no hit: it looks like evidence and
cannot be checked.

Everything runs against a mocked transport. The live API is exercised separately by
`tools/web_search.py`, because a test suite that calls a billed endpoint is a test suite people disable.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent import web_research  # noqa: E402

CONFIG = web_research._Config(base_url="https://api.test", api_key="k", timeout_seconds=5)


class _Settings:
    """The two fields config_from_settings reads."""

    model_api_key = "k"
    model_base_url = "https://api.test"


SETTINGS = _Settings()


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _reply(payload):
    return _Response(json.dumps(payload).encode())


def test_a_hit_carries_everything_needed_to_cite_it(monkeypatch):
    monkeypatch.setattr(
        web_research.urllib.request,
        "urlopen",
        lambda *a, **k: _reply(
            {
                "results": [
                    {
                        "title": "Partner Registration",
                        "url": "https://supportcso.unpartnerportal.org/hc/en-us/articles/274",
                        "summary": "Step-by-step registration",
                        "snippet": "Register your organisation",
                        "site_name": "UN Partner Portal",
                        "date": "2026-06-01T00:00:00+00:00",
                        "rerank_score": 2.52,
                        "authority": 3,
                    }
                ]
            }
        ),
    )

    hits = web_research.search("unpp registration", config=CONFIG)

    assert len(hits) == 1
    hit = hits[0]
    assert hit.url.startswith("https://")
    assert hit.score == pytest.approx(2.52)
    assert hit.authority == 3
    assert "https://" in hit.cite()


def test_a_result_without_a_url_is_dropped(monkeypatch):
    """Uncitable is not research. Passing a blank row on would put a source-shaped hole in a note."""
    monkeypatch.setattr(
        web_research.urllib.request,
        "urlopen",
        lambda *a, **k: _reply(
            {
                "results": [
                    {"title": "No url", "summary": "x"},
                    {"url": "https://ok.example", "summary": "x"},
                    {"title": "No url either", "url": ""},
                ]
            }
        ),
    )

    hits = web_research.search("q", config=CONFIG)

    assert [h.url for h in hits] == ["https://ok.example"]
    assert hits[0].title  # a missing title falls back to something non-empty


def test_an_empty_result_set_is_not_an_error(monkeypatch):
    """"Nothing found" and "could not ask" are different answers to whoever reads the research."""
    monkeypatch.setattr(web_research.urllib.request, "urlopen", lambda *a, **k: _reply({"results": []}))
    assert web_research.search("q", config=CONFIG) == []


def test_a_bad_request_is_not_retried(monkeypatch):
    """A 4xx cannot be fixed by asking again, and every attempt spends billed quota."""
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise web_research.urllib.error.HTTPError(
            "https://api.test/x", 400, "Bad Request", {}, io.BytesIO(b'{"error":"bad"}')
        )

    monkeypatch.setattr(web_research.urllib.request, "urlopen", boom)
    with pytest.raises(web_research.WebResearchError):
        web_research.search("q", config=CONFIG)
    assert len(calls) == 1


def test_a_rate_limit_is_retried_once_then_given_up(monkeypatch):
    calls = []

    def limited(*a, **k):
        calls.append(1)
        raise web_research.urllib.error.HTTPError(
            "https://api.test/x", 429, "Too Many Requests", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr(web_research.urllib.request, "urlopen", limited)
    with pytest.raises(web_research.WebResearchError):
        web_research.search("q", config=CONFIG)
    assert len(calls) == 2, "one retry on a transient status, then stop"


def test_without_a_key_it_refuses_before_spending_anything(monkeypatch):
    def never(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("no request should be made without a key")

    monkeypatch.setattr(web_research.urllib.request, "urlopen", never)
    with pytest.raises(web_research.WebResearchUnavailable):
        web_research.search("q", config=web_research._Config(api_key=""))


def test_a_fetched_page_is_truncated_and_says_so(monkeypatch):
    monkeypatch.setattr(
        web_research.urllib.request,
        "urlopen",
        lambda *a, **k: _reply({"title": "Big page", "content": "x" * 5000}),
    )
    page = web_research.fetch("https://example.org/a", max_chars=1000, config=CONFIG)
    assert len(page.content) == 1000
    assert page.truncated is True


def test_a_fetch_needs_an_http_url():
    with pytest.raises(web_research.WebResearchError):
        web_research.fetch("file:///etc/passwd", config=CONFIG)


def test_a_workflow_never_fails_because_research_did(monkeypatch):
    """An opportunity is still real and still due when the search API is down. The caller gets the
    reason, so "no research because the search failed" is distinguishable from "nothing out there".
    """
    monkeypatch.setattr(
        web_research.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down")),
    )
    hits, error = web_research.search_or_none("q", settings=SETTINGS)
    assert hits == []
    assert error and "network down" in error


def test_a_page_that_cannot_be_read_does_not_lose_the_hits(monkeypatch):
    """Live measurement: the UN portal's PDF attachments return 404 from Fetch. The search still
    answered, so dropping the whole result would throw away the one useful document."""
    state = {"n": 0}

    def responder(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            return _reply({"results": [{"title": "Hit", "url": "https://ok.example"}]})
        raise web_research.urllib.error.HTTPError(
            "https://api.test/f", 404, "Not Found", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr(web_research.urllib.request, "urlopen", responder)
    outcome = web_research.research_question("q", settings=SETTINGS)
    assert outcome["ok"] is True
    assert len(outcome["hits"]) == 1
    assert outcome["pages"][0]["error"]
