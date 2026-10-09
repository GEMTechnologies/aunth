"""A controlled application portal for browser-executor evaluation. NOT for real donors.

WHY THIS EXISTS
---------------
The directive requires comparing Stagehand Python against Browser Use Python on *measured outcomes*
rather than vendor claims. That is impossible without a portal that behaves like the real thing and
can be altered deliberately - so this fixture is the instrument, not a convenience.

It is a real HTTP service, not a mock object, because the thing under test is a browser: a stubbed
form would let a broken selector pass.

FICTIONAL IDENTITIES ONLY. Everything here is invented. `is_simulated` is stamped on every receipt so
a simulated confirmation can never be mistaken for production submission evidence - the directive
requires those be "strictly separated", and a flag in the payload is the cheapest way to make the
separation machine-checkable rather than a matter of care.

COMPLEXITY IS THE POINT. A portal that is one form with three fields tests nothing. This one has the
features that actually break browser agents: conditional questions that appear only after another
answer, a file upload, a required declaration, server-side validation that rejects a submission and
names the field, deliberate session expiry, and duplicate-submission protection that refuses a second
submission for the same reference.

THE LAYOUT VARIANTS are what test adaptability. `?variant=b` reorders the fields, renames the labels
and moves the submit button. An agent driven by fixed selectors fails on b; an agent that reads the
page does not. That difference is the single most useful measurement this fixture produces.

Uses only the standard library: no dependency is added to Granada to run its own test portal.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

#: Marks every artefact of this fixture. Anything downstream that reports a submission MUST check
#: this before treating a receipt as real.
SIMULATED = True

#: Fictional accounts. A real portal would never ship credentials in source; this one must be
#: reproducible so two adapters can be compared on identical data.
SEED_USERS: dict[str, str] = {
    "ngo.test@example.invalid": "correct-horse-battery",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Application:
    """One in-progress application. Stateful, because multi-page forms are."""

    reference: str
    email: str
    step: int = 1
    fields: dict[str, str] = field(default_factory=dict)
    #: Files are recorded by name and size, never by content: this fixture must not become a place
    #: where a real document ends up.
    uploads: dict[str, int] = field(default_factory=dict)
    declared: bool = False
    submitted: bool = False
    receipt: Optional[str] = None
    created_at: datetime = field(default_factory=_now)


class PortalState:
    """In-memory state, guarded by a lock.

    The server is threaded, and two adapters may be compared concurrently - so an unguarded dict
    would produce a flaky result that looks like an agent failure.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.users: dict[str, str] = dict(SEED_USERS)
        self.sessions: dict[str, str] = {}          # token -> email
        self.applications: dict[str, Application] = {}
        #: reference -> receipt. The duplicate guard.
        self.receipts: dict[str, str] = {}
        self.session_ttl_requests = 0               # 0 = no expiry

    def register(self, email: str, password: str) -> bool:
        with self._lock:
            if email in self.users:
                return False
            self.users[email] = password
            return True

    def login(self, email: str, password: str) -> Optional[str]:
        with self._lock:
            if self.users.get(email) != password:
                return None
            token = secrets.token_urlsafe(16)
            self.sessions[token] = email
            return token

    def expire_all_sessions(self) -> None:
        """Deliberate session expiry, so an adapter's recovery can be measured."""
        with self._lock:
            self.sessions.clear()

    def user_for(self, token: str) -> Optional[str]:
        with self._lock:
            return self.sessions.get(token)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _page(title: str, body: str) -> bytes:
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title></head>
<body>
<h1>{title}</h1>
{body}
<hr><p><small>Simulated portal. Fictional data only. Not a real funder.</small></p>
</body></html>"""
    return html.encode("utf-8")


def _login_page(error: str = "") -> bytes:
    return _page(
        "Sign in",
        f"""{'<p class="error" role="alert">' + error + '</p>' if error else ''}
<form method="post" action="/login">
  <label for="email">Email address</label>
  <input id="email" name="email" type="email" required>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" required>
  <button type="submit" id="sign-in">Sign in</button>
</form>
<p>No account? <a href="/register">Register</a></p>""",
    )


def _register_page(error: str = "") -> bytes:
    return _page(
        "Create an account",
        f"""{'<p class="error" role="alert">' + error + '</p>' if error else ''}
<form method="post" action="/register">
  <label for="email">Email address</label>
  <input id="email" name="email" type="email" required>
  <label for="password">Choose a password</label>
  <input id="password" name="password" type="password" required>
  <button type="submit" id="create-account">Create account</button>
</form>""",
    )


def _application_page(app: Application, *, variant: str, errors: dict[str, str]) -> bytes:
    """Step 1 of the application.

    THE TWO VARIANTS ARE THE ADAPTABILITY TEST. `a` is the documented layout. `b` renames every
    label, reorders the fields and moves the button - so an agent that memorised selectors fails.
    """
    def err(name: str) -> str:
        return f'<span class="field-error" data-field="{name}">{errors[name]}</span>' if name in errors else ""

    if variant == "b":
        # Reordered, relabelled, and the button is at the TOP. Same semantics, different page.
        body = f"""<form method="post" action="/apply/step1?variant=b">
  <button type="submit" id="continue">Continue to next section</button>
  <p><label for="f_org">Legal name of your organisation</label>
     <input id="f_org" name="organisation_name" type="text" required>{err('organisation_name')}</p>
  <p><label for="f_country">Country of registration</label>
     <input id="f_country" name="country" type="text" required>{err('country')}</p>
  <p><label for="f_amt">Amount requested (USD)</label>
     <input id="f_amt" name="amount" type="text">{err('amount')}</p>
</form>"""
    else:
        body = f"""<form method="post" action="/apply/step1">
  <p><label for="organisation_name">Organisation name</label>
     <input id="organisation_name" name="organisation_name" type="text" required>{err('organisation_name')}</p>
  <p><label for="country">Country</label>
     <input id="country" name="country" type="text" required>{err('country')}</p>
  <p><label for="amount">Requested amount (USD)</label>
     <input id="amount" name="amount" type="text">{err('amount')}</p>
  <button type="submit" id="continue">Continue</button>
</form>"""
    return _page(f"Application {app.reference} - Section 1 of 3", body)


def _step2_page(app: Application, *, errors: dict[str, str]) -> bytes:
    """Step 2 carries the CONDITIONAL question and the upload.

    `has_partner` controls whether the partner-name field is required. An agent that fills every
    field it can see, without reading the condition, will submit a value the portal did not ask for -
    which is exactly the failure mode this step exists to expose.
    """
    def err(name: str) -> str:
        return f'<span class="field-error" data-field="{name}">{errors[name]}</span>' if name in errors else ""

    return _page(
        f"Application {app.reference} - Section 2 of 3",
        f"""<form method="post" action="/apply/step2" enctype="multipart/form-data">
  <p><label for="summary">Project summary</label><br>
     <textarea id="summary" name="summary" rows="4" required>{app.fields.get('summary', '')}</textarea>{err('summary')}</p>

  <fieldset>
    <legend>Does your organisation work with a partner organisation?</legend>
    <label><input type="radio" name="has_partner" value="no" id="partner-no"> No</label>
    <label><input type="radio" name="has_partner" value="yes" id="partner-yes"> Yes</label>
  </fieldset>

  <p id="partner-block" hidden>
    <label for="partner_name">Partner organisation name (required if you answered Yes)</label>
    <input id="partner_name" name="partner_name" type="text">{err('partner_name')}</p>

  <p><label for="budget_file">Upload your budget (PDF)</label>
     <input id="budget_file" name="budget_file" type="file" accept="application/pdf,.pdf" required>{err('budget_file')}</p>

  <button type="submit" id="continue2">Continue</button>
</form>""",
    )


def _step3_page(app: Application, *, errors: dict[str, str]) -> bytes:
    def err(name: str) -> str:
        return f'<span class="field-error" data-field="{name}">{errors[name]}</span>' if name in errors else ""

    return _page(
        f"Application {app.reference} - Section 3 of 3",
        f"""<form method="post" action="/apply/submit">
  <h2>Review</h2>
  <dl>
    <dt>Organisation</dt><dd>{app.fields.get('organisation_name', '')}</dd>
    <dt>Country</dt><dd>{app.fields.get('country', '')}</dd>
    <dt>Amount</dt><dd>{app.fields.get('amount', '') or 'not stated'}</dd>
    <dt>Partner</dt><dd>{app.fields.get('partner_name') or 'none'}</dd>
    <dt>Budget file</dt><dd>{', '.join(app.uploads) or 'none'}</dd>
  </dl>
  <p><label><input type="checkbox" name="declaration" value="yes" id="declaration">
     I declare that the information given is accurate.</label>{err('declaration')}</p>
  <button type="submit" id="submit-application">Submit application</button>
</form>""",
    )


def _receipt_page(app: Application) -> bytes:
    return _page(
        "Application submitted",
        f"""<div id="confirmation" role="status">
  <p>Thank you. Your application has been received.</p>
  <p>Your reference is <strong id="receipt-reference">{app.receipt}</strong>.</p>
</div>
<p><small>SIMULATED RECEIPT - not production submission evidence.</small></p>""",
    )


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------
class PortalHandler(BaseHTTPRequestHandler):
    server_version = "GranadaTestPortal/1.0"
    state: PortalState

    def log_message(self, *args: Any) -> None:  # noqa: D102
        # Quiet by default: the adapter comparison measures the browser, and a chatty test server
        # makes the transcript unreadable.
        if getattr(self.server, "verbose", False):
            super().log_message(*args)

    # -- helpers ------------------------------------------------------------
    def _send(self, body: bytes, status: int = 200, cookies: Optional[dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (cookies or {}).items():
            self.send_header("Set-Cookie", f"{name}={value}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _session_email(self) -> Optional[str]:
        raw = self.headers.get("Cookie", "")
        match = re.search(r"session=([^;]+)", raw)
        return self.state.user_for(match.group(1)) if match else None

    def _application_for(self, email: str) -> Optional[Application]:
        """The most recent application for this user, SUBMITTED OR NOT.

        Excluding submitted applications looked harmless and made the duplicate guard unreachable:
        once `submitted` was set, this returned None, the second submission POST hit "No application",
        and the guard that returns the FIRST receipt never ran.

        That guard is the whole reason §11 exists - it is what lets a recovering agent discover that
        an earlier attempt already succeeded instead of filing a second application with the same
        funder. A fixture that cannot answer "did my last attempt land?" cannot test the one property
        that matters most.
        """
        candidates = [a for a in self.state.applications.values() if a.email == email]
        if not candidates:
            return None
        return max(candidates, key=lambda a: a.created_at)

    def _form(self, body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, int]]:
        """A minimal multipart and urlencoded reader. Deliberately simple: it parses what a browser
        sends, and nothing more."""
        fields: dict[str, str] = {}
        uploads: dict[str, int] = {}
        if content_type.startswith("multipart/form-data"):
            boundary = content_type.split("boundary=", 1)[-1].encode()
            for part in body.split(b"--" + boundary):
                if b"\r\n\r\n" not in part:
                    continue
                head, _, content = part.partition(b"\r\n\r\n")
                name_match = re.search(rb'name="([^"]+)"', head)
                if not name_match:
                    continue
                name = name_match.group(1).decode()
                filename_match = re.search(rb'filename="([^"]*)"', head)
                if filename_match:
                    if filename_match.group(1):
                        uploads[filename_match.group(1).decode()] = max(0, len(content) - 2)
                else:
                    fields[name] = content.rstrip(b"\r\n").decode("utf-8", "replace")
        else:
            for key, values in parse_qs(body.decode("utf-8", "replace")).items():
                fields[key] = values[-1]
        return fields, uploads

    # -- routes -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        variant = parse_qs(url.query).get("variant", ["a"])[0]
        path = url.path

        if path == "/health":
            self._json({"ok": True, "simulated": SIMULATED})
            return
        if path == "/receipts":
            self._json({"simulated": SIMULATED, "receipts": dict(self.state.receipts)})
            return
        if path in ("/", "/login"):
            self._send(_login_page())
            return
        if path == "/register":
            self._send(_register_page())
            return

        email = self._session_email()
        if email is None and path.startswith("/apply"):
            # An expired session lands here. An adapter must recognise this and re-authenticate
            # rather than treating the login page as a form it failed to fill.
            self._send(_login_page("Your session has expired. Please sign in again."), status=200)
            return

        app = self._application_for(email) if email else None
        if path == "/apply/start":
            assert email
            app = Application(reference="SIM-" + secrets.token_hex(4).upper(), email=email)
            self.state.applications[app.reference] = app
            self._send(_application_page(app, variant=variant, errors={}))
            return
        if app is None:
            self._send(_page("No application", '<p>No application in progress. <a href="/apply/start">Start one</a>.</p>'))
            return
        if path == "/apply/step2" or app.step == 2:
            self._send(_step2_page(app, errors={}))
        elif path == "/apply/step3" or app.step >= 3:
            self._send(_step3_page(app, errors={}))
        else:
            self._send(_application_page(app, variant=variant, errors={}))

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        fields, uploads = self._form(body, self.headers.get("Content-Type", ""))
        variant = parse_qs(url.query).get("variant", ["a"])[0]
        path = url.path

        if path == "/register":
            if not fields.get("email") or not fields.get("password"):
                self._send(_register_page("Email and password are both required."))
                return
            self.state.register(fields["email"], fields["password"])
            self._send(_page("Account created", '<p>You can now <a href="/login">sign in</a>.</p>'))
            return

        if path == "/login":
            token = self.state.login(fields.get("email", ""), fields.get("password", ""))
            if token is None:
                self._send(_login_page("Those credentials were not recognised."))
                return
            self._send(
                _page("Signed in", '<p><a href="/apply/start">Start an application</a></p>'),
                cookies={"session": token},
            )
            return

        email = self._session_email()
        if email is None:
            self._send(_login_page("Your session has expired. Please sign in again."))
            return
        app = self._application_for(email)
        if app is None:
            self._send(_page("No application", '<p><a href="/apply/start">Start one</a>.</p>'))
            return

        if path == "/apply/step1":
            errors = {}
            if not fields.get("organisation_name"):
                errors["organisation_name"] = "Organisation name is required."
            if not fields.get("country"):
                errors["country"] = "Country is required."
            if errors:
                # A DECLINED SUBMISSION NAMING THE FIELD. An adapter that ignores this and proceeds
                # has not validated anything.
                self._send(_application_page(app, variant=variant, errors=errors), status=200)
                return
            app.fields.update({k: v for k, v in fields.items() if k in ("organisation_name", "country", "amount")})
            app.step = 2
            self._send(_step2_page(app, errors={}))
            return

        if path == "/apply/step2":
            errors = {}
            if not fields.get("summary"):
                errors["summary"] = "A project summary is required."
            if fields.get("has_partner") == "yes" and not fields.get("partner_name"):
                errors["partner_name"] = "A partner name is required when you answer Yes."
            if not uploads:
                errors["budget_file"] = "A budget document must be attached."
            if errors:
                self._send(_step2_page(app, errors=errors), status=200)
                return
            app.fields.update({k: v for k, v in fields.items() if k in ("summary", "has_partner", "partner_name")})
            app.uploads.update(uploads)
            app.step = 3
            self._send(_step3_page(app, errors={}))
            return

        if path == "/apply/submit":
            # STEP GATE. Without this the portal accepted a submission from section 1 - a receipt
            # for an application whose page 2 and 3 were never filled in. The fixture exists to make
            # an agent prove it completed the flow, so it must refuse a submission that did not.
            if app.step < 3:
                self._send(
                    _page(
                        "Application incomplete",
                        '<p class="error" role="alert">You must complete every section before '
                        'submitting.</p><p><a href="/apply/step1">Return to section 1</a></p>',
                    ),
                    status=409,
                )
                return
            if fields.get("declaration") != "yes":
                self._send(_step3_page(app, errors={"declaration": "You must accept the declaration."}))
                return
            app.declared = True

            # DUPLICATE-SUBMISSION PROTECTION. A second submission for the same reference is refused
            # with the FIRST receipt - which is what lets a recovering agent discover that an
            # earlier attempt already succeeded instead of filing twice.
            if app.reference in self.state.receipts:
                app.receipt = self.state.receipts[app.reference]
                app.submitted = True
                self._send(_receipt_page(app))
                return

            receipt = "SIM-RCPT-" + secrets.token_hex(6).upper()
            self.state.receipts[app.reference] = receipt
            app.receipt = receipt
            app.submitted = True
            self._send(_receipt_page(app))
            return

        self.send_error(404)


def make_server(port: int = 0, *, state: Optional[PortalState] = None, verbose: bool = False) -> ThreadingHTTPServer:
    """Start the portal. `port=0` picks a free port, which is what tests should use."""
    handler = type("BoundPortalHandler", (PortalHandler,), {"state": state or PortalState()})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def serve_in_thread(port: int = 0, **kwargs: Any) -> tuple[ThreadingHTTPServer, str]:
    """Run the portal on a daemon thread and return `(server, base_url)`.

    Bound to 127.0.0.1 only. A test fixture has no business listening on a public interface, and the
    directive's SSRF requirement applies to Granada's own fixtures first.
    """
    server = make_server(port, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, actual = server.server_address[0], server.server_address[1]
    return server, f"http://{host}:{actual}"


if __name__ == "__main__":  # pragma: no cover - manual use
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    srv = make_server(port, verbose=True)
    print(f"Simulated application portal on http://127.0.0.1:{port}")
    print("Fictional data only. Not a real funder. Press Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
