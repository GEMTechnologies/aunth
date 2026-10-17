"""Funding discovery: turning live external sources into producer deliveries.

THE GAP THIS FILLS. `opportunity_ingestion` is a *receiver*: it validates and stores whatever a
producer hands it, and its docstring concedes that "a real scraper will miss" fields. Nothing in the
system ever fetched a funding call from anywhere, so the catalogue only ever contained fixtures and
no search bot existed. This module is the missing producer half.

WHAT A SOURCE MUST GUARANTEE, and why each one is a failure mode rather than a nicety:

* **Bounded.** A source is fetched on a schedule, so an unbounded fetch means one bad response can
  exhaust the process. Every call takes a page size and a request budget.
* **Isolated.** A source that is down, slow, rate-limited or has changed its schema must degrade to
  "this sweep found nothing" - never to a crash that stops the other sources and the dispatch sweep
  with them.
* **Honest.** A field the source does not supply stays absent. Fabricating a country, a deadline or
  an amount to satisfy a required column is how an NGO ends up matched against a programme it cannot
  apply to, so a source that cannot supply a required field does not emit the record at all.

THE CONTRACT IS THE INGESTION ONE. A source produces `RawOpportunity` - the exact shape
`OpportunityIngestionAdapter` validates - so discovery needs no second schema and no translation
layer that could drift from what the adapter expects.

WHY content_hash IS COMPUTED HERE. The model treats `content_hash` as the *producer's* opaque
identity and the adapter deliberately never recomputes it. This module is the producer for the
sources it implements, so it owns that normalisation - and it keeps it to the source's own stable
identifier so a re-listing of the same call is an update rather than a second opportunity.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from agent.opportunity_ingestion import RawOpportunity

logger = logging.getLogger("opportunity_sources")

#: Sent on every request. Some funding APIs reject a default Python user agent outright, and an
#: operator seeing this string in a source's logs should be able to find out who asked.
USER_AGENT = "GranadaBot/1.0 (+https://granadac.com; NGO funding discovery)"

#: A single request may not hang a sweep. Sources are refetched on a schedule, so a slow request is
#: better abandoned than allowed to hold the dispatcher's interval open.
REQUEST_TIMEOUT_SECONDS = 30.0

#: Hard cap on records produced by one sweep of one source. A source is not allowed to decide how
#: much work a sweep is worth.
MAX_PER_SWEEP = 100


class DiscoveryError(RuntimeError):
    """A source could not be reached or returned something unusable.

    Raised rather than returning an empty list, because "the source is down" and "the source has no
    new opportunities" are different facts: the first should be visible to an operator, the second is
    ordinary. The caller logs the former and carries on.
    """


@dataclass(frozen=True)
class SourceFetch:
    """What one sweep of one source produced, including why it produced nothing."""

    source_name: str
    opportunities: list[RawOpportunity] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ok(self) -> bool:
        return not self.errors


def _strip_html(value: Optional[str]) -> Optional[str]:
    """Plain text from a fragment, for fields the adapter stores but humans read.

    Not a sanitiser and not used for rendering: the value is stored for matching and for a person
    reading a listing, so tags are removed and entities decoded rather than escaped.
    """
    if not value:
        return None
    text = re.sub(r"<br\s*/?>", "\n", str(value), flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, replacement in (
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'"),
    ):
        text = text.replace(entity, replacement)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None


def _parse_us_date(value: Optional[str]) -> Optional[datetime]:
    """Parse the several date shapes federal and EU feeds use, or return None.

    Returning None rather than raising, because a deadline is optional on the model and an
    unparseable one must not cost the whole record. It must also not be INVENTED: a fabricated
    deadline makes an opportunity look closable when it is not.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Compact US shape used by the grants.gov search endpoint: MM/DD/YYYY.
    for shape in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, shape).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Verbose shape used by the detail endpoint: "Jan 11, 2027 12:00:00 AM EST".
    cleaned = re.sub(r"\s+[A-Z]{2,5}$", "", text)  # trailing timezone name
    for shape in ("%b %d, %Y %I:%M:%S %p", "%B %d, %Y %I:%M:%S %p", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(cleaned.strip(), shape).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _to_int(value: Any) -> Optional[int]:
    """An integer amount, or None. Never a guess from a formatted string like "3,500,000"."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[^0-9.]", "", str(value))
    if not digits:
        return None
    try:
        return int(float(digits))
    except ValueError:  # pragma: no cover - defensive
        return None


def _bounded(value: Optional[str], width: int) -> Optional[str]:
    """Fit a free-text value into a bounded column, or None.

    SOURCES DO NOT RESPECT OUR COLUMN WIDTHS, AND THE DATABASE IS THE WRONG PLACE TO FIND OUT.
    Measured live: grants.gov's funding-activity category is the sentence "Other (see text field
    entitled "Explanation of Other Category of Funding Activity" for clarification)", which is 108
    characters against a varchar(100) - so a single such record aborted the whole sweep with
    `StringDataRightTruncation` after every other opportunity in the batch had been validated.

    Truncating here is the smaller evil: the alternative is either dropping an otherwise good
    opportunity because one descriptive field is long, or raising the column width for a value
    nobody filters on. The full text is still retained in `raw_payload`, so nothing is lost.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text[:width] if text else None


def _looks_like_email(value: Optional[str]) -> bool:
    """Whether a source's "contact email" is actually an email.

    Measured live: grants.gov returns `http://www.eda.gov/contact/` in `agencyContactEmail` for some
    records. Storing that in an email column means every later consumer - reputation checks, mail
    drafting, dedupe by domain - treats a web form as an address, so a value that is not an address
    is kept as None and the description retains the link.
    """
    if not value:
        return False
    text = str(value).strip()
    if "@" not in text or text.lower().startswith(("http://", "https://")):
        return False
    local, _, domain = text.rpartition("@")
    return bool(local) and "." in domain and " " not in text


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


class GrantsGovSource:
    """US federal funding opportunities from the Grants.gov search API.

    WHY THIS SOURCE. It answers without an API key, returns structured JSON, and covers a large
    volume of grant programmes that international NGOs legitimately apply to. It is the reference
    implementation of the contract; a second source should copy its shape rather than its URLs.

    TWO CALLS PER RECORD, SO DETAIL IS BUDGETED. The search endpoint lists identifiers and dates;
    amounts, eligibility and the description live on a per-opportunity detail call. Detail is
    therefore fetched only for records actually being emitted, capped by ``with_detail_limit``, so a
    large page cannot turn into a hundred sequential requests.
    """

    NAME = "Grants.gov"
    SEARCH_URL = "https://api.grants.gov/v1/api/search2"
    DETAIL_URL = "https://api.grants.gov/v1/api/fetchOpportunity"
    #: Verified live: returns 200 and a JSON body. Kept as a template so the URL in the catalogue
    #: always points at the notice a person would read.
    PUBLIC_URL = "https://www.grants.gov/search-results-detail/{id}"

    def __init__(
        self,
        *,
        keywords: Iterable[str] = ("community development",),
        statuses: str = "posted",
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        opener: Any = None,
    ) -> None:
        self.keywords = [k for k in (keywords or ()) if str(k).strip()]
        self.statuses = statuses
        self.timeout = timeout
        #: Injectable so tests exercise the mapping without the network, and so a different HTTP
        #: client can be substituted in a deployment that needs proxy support.
        self._opener = opener or self._post

    # -- transport ------------------------------------------------------
    def _post(self, url: str, body: dict) -> dict:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise DiscoveryError(f"{url} returned HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001 - network faults are all one class here
            raise DiscoveryError(f"{url} could not be reached: {type(exc).__name__}") from exc
        try:
            return json.loads(payload)
        except ValueError as exc:
            raise DiscoveryError(f"{url} returned non-JSON: {payload[:120]!r}") from exc

    # -- contract -------------------------------------------------------
    def fetch(
        self, *, limit: int = 25, with_detail: bool = True, with_detail_limit: int = 15
    ) -> SourceFetch:
        """One bounded sweep. Never raises: a failure becomes an error on the result."""
        limit = max(1, min(int(limit or 1), MAX_PER_SWEEP))
        errors: list[str] = []
        keywords = self.keywords or [""]

        hits: list[dict] = []
        seen_ids: set[str] = set()
        # The budget is split across keywords so three keywords cannot triple the sweep.
        per_keyword = max(1, limit // len(keywords))
        for keyword in keywords:
            try:
                body = self._opener(
                    self.SEARCH_URL,
                    {"keyword": keyword, "oppStatuses": self.statuses, "rows": per_keyword},
                )
            except DiscoveryError as exc:
                errors.append(f"{keyword or '(all)'}: {exc}")
                continue

            if str(body.get("errorcode", "0")) not in ("0", "None", ""):
                errors.append(f"{keyword or '(all)'}: {body.get('msg') or 'source reported an error'}")
                continue
            if body.get("errorMsgs"):
                errors.append(f"{keyword or '(all)'}: {body['errorMsgs']}")

            for hit in ((body.get("data") or {}).get("oppHits") or []):
                identifier = str(hit.get("id") or "").strip()
                if not identifier or identifier in seen_ids:
                    continue
                seen_ids.add(identifier)
                hits.append(hit)

        # Detail is fetched breadth-first so a slow detail call cannot starve every record of one.
        details: dict[str, dict] = {}
        if with_detail:
            for hit in hits[: max(0, int(with_detail_limit))]:
                identifier = str(hit.get("id"))
                try:
                    body = self._opener(self.DETAIL_URL, {"opportunityId": int(identifier)})
                except (DiscoveryError, ValueError) as exc:
                    errors.append(f"detail {identifier}: {exc}")
                    continue
                data = body.get("data") if isinstance(body, dict) else None
                if isinstance(data, dict):
                    details[identifier] = data

        opportunities: list[RawOpportunity] = []
        for hit in hits[:limit]:
            built = self._to_raw(hit, details.get(str(hit.get("id"))))
            if built is None:
                errors.append(f"{hit.get('id')}: could not be represented as an opportunity")
                continue
            opportunities.append(built)

        return SourceFetch(source_name=self.NAME, opportunities=opportunities, errors=errors)

    # -- mapping --------------------------------------------------------
    def _to_raw(self, hit: dict, detail: Optional[dict]) -> Optional[RawOpportunity]:
        """Map one hit, or return None when it cannot satisfy the ingestion contract.

        The required columns are title, source_url, source_name, country and content_hash. A record
        missing a title has no value to a reader and no stable identity, so it is dropped rather
        than emitted with a placeholder.
        """
        title = _strip_html(hit.get("title")) or None
        identifier = str(hit.get("id") or "").strip()
        if not title or not identifier:
            return None

        synopsis = (detail or {}).get("synopsis") or {}
        number = str(hit.get("number") or synopsis.get("opportunityNumber") or "").strip() or None

        # THE PRODUCER OWNS THIS HASH. Built from the source's own stable identifier only, so a
        # re-listing, a corrected title or an extended deadline updates the existing row instead of
        # creating a second opportunity for the same programme.
        content_hash = hashlib.sha256(f"grants.gov|{identifier}".encode("utf-8")).hexdigest()

        deadline = (
            _parse_us_date(hit.get("closeDate"))
            or _parse_us_date(synopsis.get("responseDateStr"))
            or _parse_us_date(synopsis.get("responseDate"))
        )
        award_min = _to_int(synopsis.get("awardFloor"))
        award_max = _to_int(synopsis.get("awardCeiling"))
        if award_min is not None and award_max is not None and award_min > award_max:
            # The adapter rejects this pair outright, which would cost the whole record. Keep the
            # ceiling - it is the number a reader acts on - and drop the contradictory floor.
            award_min = None

        agency = (hit.get("agency") or synopsis.get("agencyName") or "").strip() or None
        categories = [c.get("description") for c in (synopsis.get("fundingActivityCategories") or [])
                      if isinstance(c, dict)]
        eligibilities = [e.get("description") for e in (synopsis.get("applicantTypes") or [])
                         if isinstance(e, dict)]
        eligibility = _strip_html(synopsis.get("applicantEligibilityDesc")) or (
            "; ".join(e for e in eligibilities if e) or None
        )

        return RawOpportunity(
            title=title[:500],
            source_url=self.PUBLIC_URL.format(id=identifier),
            source_name=self.NAME,
            # Federal programmes are administered by a US agency; the geography a programme serves
            # is in the description and is not invented here.
            country="United States",
            content_hash=content_hash,
            description=_strip_html(synopsis.get("synopsisDesc")),
            deadline=deadline,
            amount_min=award_min,
            amount_max=award_max,
            currency="USD" if (award_min or award_max) else None,
            # Every free-text field is bounded to its column width; see `_bounded` for why a long
            # descriptive value must not be able to reject an otherwise good opportunity.
            sector=_bounded(categories[0] if categories else None, 100),
            eligibility_criteria=eligibility,
            application_process=_strip_html(
                synopsis.get("fundingDescLinkDesc") or synopsis.get("fundingDescLinkUrl")
            ),
            contact_email=(
                _bounded(synopsis.get("agencyContactEmail"), 200)
                if _looks_like_email(synopsis.get("agencyContactEmail"))
                else None
            ),
            contact_phone=_bounded(synopsis.get("agencyContactPhone"), 50),
            # JSON columns: the raw code lists are what a matcher can compare against.
            keywords={"cfda": _as_list(hit.get("cfdaList")), "number": number,
                      "agency_code": hit.get("agencyCode"), "doc_type": hit.get("docType")},
            focus_areas={"categories": [c for c in categories if c],
                         "applicant_types": [e for e in eligibilities if e]},
            is_active=str(hit.get("oppStatus") or "").strip().lower() == "posted",
            # The source's own identifier, so a row can be traced back to the notice it came from.
            source_id=identifier,
            scraped_at=datetime.now(timezone.utc),
            # RETAINED VERBATIM. The brief requires the original payload, and for a source that can
            # change its schema this is the only way to reconstruct what was seen.
            raw_payload={"search": hit, "detail": detail},
        )


#: The registry a sweep iterates. Adding a source is a decision, so a new key here is deliberate and
#: a source that is not listed cannot be scheduled by accident.
SOURCES: dict[str, Any] = {
    "grants_gov": GrantsGovSource,
}


def build_sources(
    names: Iterable[str], *, keywords: Iterable[str] = ("community development",)
) -> list[Any]:
    """Construct the named sources, skipping (and reporting) an unknown one.

    An unknown name is skipped rather than raised: a stale configuration entry must not stop a
    sweep from running the sources that do exist, which is the same reasoning as a single malformed
    delivery not abandoning a batch.
    """
    built: list[Any] = []
    for name in names:
        factory = SOURCES.get(str(name).strip())
        if factory is None:
            logger.warning("discovery_unknown_source", extra={"source": name})
            continue
        built.append(factory(keywords=keywords))
    return built


def discover(
    sources: Iterable[Any], *, limit: int = 25, with_detail: bool = True
) -> list[SourceFetch]:
    """Sweep every source, isolating failures. Never raises."""
    results: list[SourceFetch] = []
    for source in sources:
        try:
            results.append(source.fetch(limit=limit, with_detail=with_detail))
        except Exception as exc:  # noqa: BLE001 - a source must not stop the others
            results.append(
                SourceFetch(
                    source_name=getattr(source, "NAME", type(source).__name__),
                    errors=[f"{type(exc).__name__}: {exc}"],
                )
            )
    return results
