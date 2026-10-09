"""The simulated application portal is an instrument, so the instrument is tested first.

If the fixture itself is wrong, every adapter comparison run against it is meaningless - and worse,
a bug in the fixture looks like a failure of whichever agent is being measured. These tests exist so
that never happens.

The three defects found while building it, all now pinned:

1. `/apply/submit` issued a receipt from section 1. A portal that accepts an incomplete application
   cannot test whether an agent completed a flow, because it never checks.
2. The duplicate guard was unreachable: `_application_for` filtered out submitted applications, so a
   second submission hit "No application" and never returned the FIRST receipt. That guard is the
   mechanism by which a recovering agent learns its earlier attempt already landed.
3. (Test-side) a multipart upload must be sent with `multipart/form-data; boundary=...`; sending it
   as urlencoded tested nothing.
"""

from __future__ import annotations

import http.cookiejar
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(BACKEND / "tools") not in sys.path:
    sys.path.insert(0, str(BACKEND / "tools"))

from test_portal import SIMULATED, serve_in_thread  # noqa: E402


class Portal:
    """A tiny browser-less client. It drives the same HTTP the browser will drive."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def get(self, path: str) -> tuple[int, str]:
        try:
            r = self.opener.open(self.base + path, timeout=10)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def post(self, path: str, data: dict | None = None, *, raw: bytes | None = None,
             ctype: str | None = None) -> tuple[int, str]:
        if raw is None:
            raw = urllib.parse.urlencode(data or {}).encode()
            ctype = "application/x-www-form-urlencoded"
        req = urllib.request.Request(self.base + path, raw, {"Content-Type": ctype})
        try:
            r = self.opener.open(req, timeout=10)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def multipart(self, path: str, fields: dict[str, str], filename: str, content: bytes):
        boundary = "----granada-test"
        parts = []
        for name, value in fields.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
            )
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"budget_file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/pdf\r\n\r\n"
        )
        body = "".join(parts).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return self.post(
            path, raw=body, ctype=f"multipart/form-data; boundary={boundary}"
        )


@pytest.fixture
def portal():
    server, base = serve_in_thread()
    try:
        yield Portal(base), server
    finally:
        server.shutdown()


def _signed_in() -> tuple[Portal, object]:
    """A fresh portal with one fictional identity signed in."""
    server, base = serve_in_thread()
    p = Portal(base)
    p.post("/register", {"email": "ngo.fixture@example.invalid", "password": "fictional-pass"})
    p.post("/login", {"email": "ngo.fixture@example.invalid", "password": "fictional-pass"})
    return p, server


# ===========================================================================
# THE FIXTURE IS MARKED AS SIMULATED
# ===========================================================================
def test_every_artefact_is_marked_simulated():
    """The directive requires simulated receipts and production evidence be strictly separated.
    A machine-checkable flag is what makes that separation real rather than a matter of care."""
    p, server = _signed_in()
    try:
        status, body = p.get("/health")
        assert status == 200 and '"simulated": true' in body
        status, body = p.get("/receipts")
        assert '"simulated": true' in body
        assert SIMULATED is True
    finally:
        server.shutdown()


# ===========================================================================
# DEFECT 1 - the step gate
# ===========================================================================
def test_a_submission_from_section_one_is_REFUSED():
    """THE defect: the portal issued a receipt for an application whose pages 2 and 3 were never
    filled. A fixture that accepts that cannot test whether an agent completed the flow."""
    p, server = _signed_in()
    try:
        p.get("/apply/start")
        status, body = p.post("/apply/submit", {"declaration": "yes"})
        assert status == 409, f"an incomplete application was accepted (HTTP {status})"
        assert "complete every section" in body
        assert "receipt-reference" not in body
    finally:
        server.shutdown()


def test_the_full_flow_reaches_a_receipt():
    p, server = _signed_in()
    try:
        p.get("/apply/start")
        status, body = p.post(
            "/apply/step1",
            {"organisation_name": "Fictional NGO", "country": "Nigeria", "amount": "25000"},
        )
        assert "Section 2 of 3" in body
        status, body = p.multipart(
            "/apply/step2",
            {"summary": "A fictional summary.", "has_partner": "no"},
            "budget.pdf",
            b"%PDF-1.4 fictional",
        )
        assert "Section 3 of 3" in body, "the multipart upload did not advance the form"
        status, body = p.post("/apply/submit", {"declaration": "yes"})
        assert re.search(r'id="receipt-reference">([^<]+)<', body)
    finally:
        server.shutdown()


# ===========================================================================
# DEFECT 2 - the duplicate guard
# ===========================================================================
def test_a_duplicate_submission_returns_the_FIRST_receipt():
    """THE defect, and the one that models real risk.

    `_application_for` filtered out submitted applications, so the second POST never reached the
    guard and the portal could not answer "did my earlier attempt already land?" - the exact question
    a recovering browser agent must ask after a crash following Submit.
    """
    p, server = _signed_in()
    try:
        p.get("/apply/start")
        p.post("/apply/step1", {"organisation_name": "Fictional NGO", "country": "Nigeria"})
        p.multipart("/apply/step2", {"summary": "s", "has_partner": "no"}, "b.pdf", b"%PDF-1.4")
        _, first = p.post("/apply/submit", {"declaration": "yes"})
        first_receipt = re.search(r'id="receipt-reference">([^<]+)<', first)
        assert first_receipt, "no receipt on the first submission"

        status, second = p.post("/apply/submit", {"declaration": "yes"})
        second_receipt = re.search(r'id="receipt-reference">([^<]+)<', second)
        assert second_receipt, (
            "the duplicate submission did not return the earlier receipt - a recovering agent "
            "cannot discover that its first attempt already succeeded"
        )
        assert second_receipt.group(1) == first_receipt.group(1), "the portal filed a SECOND application"
    finally:
        server.shutdown()


# ===========================================================================
# VALIDATION IS REAL
# ===========================================================================
def test_a_missing_mandatory_field_is_rejected_and_NAMED():
    p, server = _signed_in()
    try:
        p.get("/apply/start")
        _, body = p.post("/apply/step1", {"organisation_name": "Fictional NGO"})
        assert 'data-field="country"' in body, "the portal did not name the missing field"
    finally:
        server.shutdown()


def test_a_conditional_field_is_required_only_when_the_condition_holds():
    """The point of the conditional question: an agent that fills every visible field without
    reading the condition submits a value the portal did not ask for."""
    p, server = _signed_in()
    try:
        p.get("/apply/start")
        p.post("/apply/step1", {"organisation_name": "Fictional NGO", "country": "Nigeria"})

        _, body = p.multipart(
            "/apply/step2", {"summary": "s", "has_partner": "yes"}, "b.pdf", b"%PDF-1.4"
        )
        assert 'data-field="partner_name"' in body, "Yes without a partner name was accepted"

        _, body = p.multipart(
            "/apply/step2",
            {"summary": "s", "has_partner": "yes", "partner_name": "Fictional Partner"},
            "b.pdf",
            b"%PDF-1.4",
        )
        assert "Section 3 of 3" in body
    finally:
        server.shutdown()


def test_the_declaration_is_required():
    p, server = _signed_in()
    try:
        p.get("/apply/start")
        p.post("/apply/step1", {"organisation_name": "Fictional NGO", "country": "Nigeria"})
        p.multipart("/apply/step2", {"summary": "s", "has_partner": "no"}, "b.pdf", b"%PDF-1.4")
        _, body = p.post("/apply/submit", {})
        assert 'data-field="declaration"' in body
    finally:
        server.shutdown()


def test_an_upload_is_required():
    p, server = _signed_in()
    try:
        p.get("/apply/start")
        p.post("/apply/step1", {"organisation_name": "Fictional NGO", "country": "Nigeria"})
        _, body = p.post("/apply/step2", {"summary": "s", "has_partner": "no"})
        assert 'data-field="budget_file"' in body
    finally:
        server.shutdown()


# ===========================================================================
# THE ADAPTABILITY TEST
# ===========================================================================
def test_variant_b_renames_and_reorders_every_field():
    """What makes the comparison meaningful. An agent driven by memorised selectors passes `a` and
    fails `b`; an agent that reads the page passes both. That difference is the measurement."""
    p, server = _signed_in()
    try:
        _, a = p.get("/apply/start")
        assert "Organisation name" in a
        assert 'id="continue"' in a

        p2, server2 = _signed_in()
        try:
            _, b = p2.get("/apply/start?variant=b")
            assert "Legal name of your organisation" in b, "variant b did not relabel the fields"
            assert "Organisation name</label>" not in b
            # The submit control is reordered too, so a positional script breaks.
            assert b.index('id="continue"') < b.index('name="organisation_name"')
        finally:
            server2.shutdown()
    finally:
        server.shutdown()


# ===========================================================================
# SESSION EXPIRY
# ===========================================================================
def test_an_expired_session_returns_the_login_page_not_the_form():
    """An adapter must recognise re-authentication is needed, rather than treating the login page as
    a form it failed to fill."""
    p, server = _signed_in()
    try:
        p.get("/apply/start")
        server.RequestHandlerClass.state.expire_all_sessions()
        status, body = p.get("/apply/start")
        assert "session has expired" in body.lower()
        assert "Section 1 of 3" not in body
    finally:
        server.shutdown()


def test_wrong_credentials_do_not_create_a_session():
    server, base = serve_in_thread()
    p = Portal(base)
    try:
        p.post("/register", {"email": "ngo.x@example.invalid", "password": "fictional-pass"})
        status, body = p.post(
            "/login", {"email": "ngo.x@example.invalid", "password": "wrong"}
        )
        assert "not recognised" in body
        _, body = p.get("/apply/start")
        assert "Section 1 of 3" not in body
    finally:
        server.shutdown()


# ===========================================================================
# THE PORTAL IS NOT PUBLIC
# ===========================================================================
def test_the_fixture_binds_to_loopback_only():
    """A test fixture has no business listening on a public interface - the directive's SSRF
    requirement applies to Granada's own fixtures first."""
    server, base = serve_in_thread()
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert base.startswith("http://127.0.0.1:")
    finally:
        server.shutdown()
