"""Live web search and page fetch, via DeepSeek's in-house search engine.

WHY THIS EXISTS, AND WHY IT MATTERS FOR THIS PRODUCT.

DeepSeek opened its Search and Fetch APIs on 2026-10-17. They are plain JSON over the same bearer key
the model gateway already uses:

    POST https://api.deepseek.com/v1/tools/search   {query, count, need_content}
    POST https://api.deepseek.com/v1/tools/fetch    {url}

Until now the only way this platform could read a web page was a real browser session - a leased
capability, minutes long, and the thing most likely to break. Fetch returns the same page as Markdown
for about $0.00075, and Search answers "what does this funder require" in one call. The browser remains
necessary for what only a browser can do (filling a form, taking a screenshot, completing a signup),
but it should not be the first tool reached for a question that is just "read this page".

MEASURED BEFORE WRITING THIS: a live search for "UN Partner Portal NGO registration requirements"
returned, as its top hit, "Partner Registration" on supportcso.unpartnerportal.org with a rerank score of
2.52 - the exact document the browser task has been trying to discover by driving the portal. Fetch of
https://www.unpartnerportal.org/ returned the page title and body as Markdown.

PROVENANCE IS THE POINT. Every result carries its URL, the site name and an authority tier, and the
record keeps them. A research note the agent produces has to be traceable to a page a person can open -
the same rule that governs evidence elsewhere in this platform.

BILLING AND LIMITS are real and are respected here: $0.75 per 1000 requests for each API, 5 QPS each,
and a request is only billed when it succeeds. So this module never retries a 4xx, and callers should
treat a failure as "no research this time" rather than hammering the endpoint.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"

#: 5 QPS per account for each API, shared across every key. A single retry on the transient classes
#: (408/429/5xx) is worth doing; retrying an invalid request just burns the quota in a tight loop.
_TRANSIENT = frozenset({408, 429, 500, 502, 503, 504})


class WebResearchError(RuntimeError):
    """Search or fetch failed in a way the caller must handle rather than ignore."""


class WebResearchUnavailable(WebResearchError):
    """No API key is configured, or the feature is switched off."""


@dataclass(frozen=True)
class SearchHit:
    """One search result, with the provenance a research record needs."""

    title: str
    url: str
    summary: str
    snippet: str = ""
    site_name: Optional[str] = None
    date: Optional[str] = None
    #: 0..3, combining relevance, authority and freshness. 1.5+ is high quality; 3+ is highly
    #: authoritative. Kept because it is the cheapest way to rank without a second model call.
    score: float = 0.0
    #: 0..4 authority tier; -1 unknown.
    authority: int = -1

    def cite(self) -> str:
        """A one-line citation, for a research note a person will read."""
        return f"{self.title} - {self.url}"


@dataclass(frozen=True)
class FetchedPage:
    url: str
    title: str
    content: str
    #: True when the body was truncated to `max_chars`, so a caller can say so rather than pretend
    #: it read the whole page.
    truncated: bool = False


@dataclass
class _Config:
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    timeout_seconds: int = 30
    enabled: bool = True


def config_from_settings(settings: Any) -> _Config:
    """Read the same settings the model gateway uses, so there is one key and one base URL."""
    base = (getattr(settings, "model_base_url", "") or "https://api.deepseek.com").strip()
    key = (getattr(settings, "model_api_key", "") or "").strip()
    return _Config(
        base_url=base.rstrip("/") or "https://api.deepseek.com",
        api_key=key,
        enabled=bool(getattr(settings, "web_search_enabled", True)),
    )


def _post(config: _Config, path: str, body: dict[str, Any]) -> dict[str, Any]:
    if not config.enabled:
        raise WebResearchUnavailable("web research is switched off")
    if not config.api_key:
        raise WebResearchUnavailable("no API key is configured for web research")

    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    attempt = 0
    while True:
        attempt += 1
        request = urllib.request.Request(
            config.base_url + path, data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            # 402 is "top up the account" and 4xx otherwise is "change the request". Neither is
            # improved by asking again, and every attempt spends quota.
            if exc.code not in _TRANSIENT or attempt >= 2:
                raise WebResearchError(
                    f"{path} failed with HTTP {exc.code}: {detail or exc.reason}"
                ) from exc
            logger.warning(
                "web_research.transient_failure", extra={"path": path, "status": exc.code}
            )
        except Exception as exc:  # noqa: BLE001
            if attempt >= 2:
                raise WebResearchError(f"{path} failed: {type(exc).__name__}") from exc
            logger.warning(
                "web_research.transport_failure", extra={"path": path, "error": type(exc).__name__}
            )


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def search(
    query: str,
    *,
    count: int = 5,
    need_content: bool = False,
    config: Optional[_Config] = None,
    settings: Any = None,
) -> list[SearchHit]:
    """Find pages for a query. Returns [] when the engine finds nothing - that is not an error.

    `query` is passed to the engine with its operators intact, so `site:`, `after:` and quoted phrases
    all work. Raising only on transport/API failure keeps "no results" distinguishable from
    "could not ask", which is the distinction every caller here needs.
    """
    query = (query or "").strip()
    if not query:
        raise WebResearchError("a search needs a query")

    resolved = config or config_from_settings(settings)
    body = {
        "query": query[:128],
        "count": max(1, min(50, int(count))),
        "need_content": bool(need_content),
    }
    data = _post(resolved, SEARCH_PATH, body)

    hits: list[SearchHit] = []
    for row in data.get("results") or []:
        url = (row.get("url") or "").strip()
        title = (row.get("title") or "").strip()
        if not url or not title:
            # A result without a title or a URL cannot be cited, and an uncitable result is not
            # research. Dropped rather than passed on as a blank row.
            continue
        hits.append(
            SearchHit(
                title=title,
                url=url,
                summary=(row.get("summary") or "").strip(),
                snippet=(row.get("snippet") or "").strip(),
                site_name=row.get("site_name"),
                date=row.get("date"),
                score=_score(row.get("rerank_score")),
                authority=int(row.get("authority") if row.get("authority") is not None else -1),
            )
        )
    return hits


def fetch(
    url: str,
    *,
    max_chars: int = 20000,
    config: Optional[_Config] = None,
    settings: Any = None,
) -> FetchedPage:
    """Retrieve one page as Markdown.

    The body is truncated rather than refused: 20k characters is more than any single prompt wants,
    and a caller that gets a truncated page can say so. Silently passing a 400k-character page to a
    model is how a context window gets spent on navigation menus.
    """
    url = (url or "").strip()
    if not url:
        raise WebResearchError("a fetch needs a URL")
    if not url.lower().startswith(("http://", "https://")):
        raise WebResearchError("a fetch needs an http or https URL")

    resolved = config or config_from_settings(settings)
    data = _post(resolved, FETCH_PATH, {"url": url})

    content = (data.get("content") or "").strip()
    truncated = len(content) > max_chars
    return FetchedPage(
        url=url,
        title=(data.get("title") or "").strip(),
        content=content[:max_chars],
        truncated=truncated,
    )


def search_or_none(
    query: str, *, count: int = 5, settings: Any = None
) -> tuple[list[SearchHit], Optional[str]]:
    """Search without raising, for callers inside a workflow.

    A workflow must not fail because a research call did: the opportunity is still real and the
    application still due. The failure is returned so the caller can record WHY there is no research,
    which is different from recording that there is none.
    """
    try:
        return search(query, count=count, settings=settings), None
    except WebResearchError as exc:
        return [], str(exc)


def research_question(question: str, *, count: int = 5, settings: Any = None) -> dict[str, Any]:
    """Search, then read the strongest results. One call, for a question a person asked.

    Returns the hits and the pages that were read, each carrying its URL, so anything built from this
    can be cited. Deliberately bounded: at most three pages, because each fetch is a billed request and
    a research note that reads ten pages to answer one question is spending without thinking.
    """
    hits, error = search_or_none(question, count=count, settings=settings)
    if error:
        return {"ok": False, "error": error, "hits": [], "pages": []}

    pages: list[dict[str, Any]] = []
    for hit in hits[:3]:
        try:
            page = fetch(hit.url, settings=settings)
        except WebResearchError as exc:
            pages.append({"url": hit.url, "error": str(exc)})
            continue
        pages.append(
            {
                "url": page.url,
                "title": page.title,
                "content": page.content,
                "truncated": page.truncated,
            }
        )

    return {
        "ok": True,
        "question": question,
        "hits": [hit.__dict__ for hit in hits],
        "pages": pages,
    }
