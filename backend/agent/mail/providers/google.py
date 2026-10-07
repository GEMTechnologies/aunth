"""Google Gmail outbound adapter.

Written from the official documentation, and **not verified against the live API** -
no credentials exist in this environment and no live send was attempted. What *is*
verified is that the request this builds matches the documented contract: endpoint,
method, authorization header, and a base64url-encoded RFC 2822 message in ``raw``.

The endpoint and scope
---------------------
``POST https://gmail.googleapis.com/gmail/v1/users/{userId}/messages/send``
with scope ``https://www.googleapis.com/auth/gmail.send``.

**``gmail.send`` is not ``gmail.readonly``, and this is where the brief's rule
lands.** A mailbox connected read-only gets an adapter whose ``capabilities`` do NOT
include ``MAIL_SEND``, and the final authority check refuses before any call. That is
why the capability set is a class attribute rather than a constructor argument a
caller could set optimistically.

The response
------------
Gmail returns ``{"id": ..., "threadId": ..., "labelIds": ["SENT"]}``. The ``id`` is a
real submission identifier, so reconciliation can look the message up directly rather
than searching.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Optional

from agent.mail.outbound import (
    OutboundMessage,
    ReconciliationResult,
    SendFailure,
    SendOutcome,
    SubmitResult,
)
from agent.mail.providers.http import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpError,
    HttpResponse,
    HttpTransport,
    HttpxTransport,
)

GMAIL_SEND_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/{user_id}/messages/send"
GMAIL_MESSAGES_GET = "https://gmail.googleapis.com/gmail/v1/users/{user_id}/messages/{message_id}"

#: The scope that permits sending. Recorded as a constant so a scope change is a
#: visible edit rather than a string buried in a request.
SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"

SEND_CAPABILITIES = frozenset({"MAIL_SEND", "MAIL_SEND_HUMAN_APPROVED", "MAIL_RECONCILE_SEND"})
READ_ONLY_CAPABILITIES = frozenset({"MAIL_READ", "MAIL_SYNC"})


def build_rfc2822(
    message: OutboundMessage, *, message_id: Optional[str] = None
) -> bytes:
    """Build the RFC 2822 message Gmail's ``raw`` field carries.

    Uses the standard library's ``EmailMessage`` rather than string concatenation.
    Hand-rolled headers get the encoding wrong on the first non-ASCII subject, and a
    mangled subject line is the kind of failure nobody notices until a funder replies
    to something that looked fine in the outbox.
    """
    rfc = EmailMessage()
    rfc["From"] = message.from_address
    rfc["To"] = ", ".join(message.to_addresses)
    if message.cc_addresses:
        rfc["Cc"] = ", ".join(message.cc_addresses)
    if message.reply_to_address:
        rfc["Reply-To"] = message.reply_to_address
    rfc["Subject"] = message.subject
    rfc["Date"] = formatdate(localtime=False)
    rfc["Message-ID"] = message_id or make_msgid(domain="granada.example")

    # Granada's own reference, as a header. It is what reconciliation looks for when
    # a provider does not hand back a usable identifier, and it is an opaque value
    # rather than a database id so the sent message does not leak a row count.
    if message.granada_message_ref:
        rfc["X-Granada-Ref"] = message.granada_message_ref
    if message.approval_fingerprint:
        # The sent message is self-describing about WHAT was authorised, which is
        # what makes "was this the approved text?" answerable from the message.
        rfc["X-Granada-Approval"] = message.approval_fingerprint
    for key, value in (message.headers or {}).items():
        rfc[key] = value

    rfc.set_content(message.body_text or "")
    for attachment in message.attachments:
        content = attachment.get("content")
        if not content:
            continue
        rfc.add_attachment(
            content,
            maintype=(attachment.get("mime_type") or "application/octet-stream").split("/")[0],
            subtype=(attachment.get("mime_type") or "application/octet-stream").split("/")[-1],
            filename=attachment.get("filename") or "attachment",
        )
    return rfc.as_bytes()


def encode_raw(payload: bytes) -> str:
    """base64url without padding, which is what the Gmail API expects."""
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


class GmailOutboundProvider:
    """Sends mail through the Gmail API."""

    name = "GOOGLE"

    def __init__(
        self,
        *,
        user_id: str = "me",
        access_token: Optional[str] = None,
        scopes: Optional[frozenset[str]] = None,
        transport: Optional[HttpTransport] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not user_id:
            raise ValueError("a Gmail adapter needs a user id")
        self.user_id = user_id
        self.access_token = access_token
        self.scopes = frozenset(scopes) if scopes is not None else frozenset({SCOPE_SEND})
        self.transport = transport or HttpxTransport()
        self.timeout = timeout

        # Capabilities are DERIVED from the granted scopes, not passed in. That is
        # the brief's rule - "READ permission must NOT imply SEND" - expressed so it
        # cannot be got wrong: a read-only connection simply has no MAIL_SEND.
        self.capabilities = (
            SEND_CAPABILITIES if SCOPE_SEND in self.scopes else READ_ONLY_CAPABILITIES
        )

    # ------------------------------------------------------------------
    def submit_message(self, *, message: OutboundMessage, idempotency_key: str) -> SubmitResult:
        """POST the raw message. Maps the response onto the three outcomes."""
        if "MAIL_SEND" not in self.capabilities:
            # Belt and braces: the final authority check refuses earlier, and this
            # exists so a direct call cannot bypass it.
            from agent.mail.outbound import OutboundCapabilityMissing

            raise OutboundCapabilityMissing(
                "this Gmail connection was granted read-only scope; no send capability"
            )
        if not self.access_token:
            return SubmitResult(
                outcome=SendOutcome.CONFIRMED_NOT_SENT,
                failure=SendFailure.AUTH_REQUIRED,
                error_code="NO_ACCESS_TOKEN",
                safe_error_summary="no OAuth access token is available for this mailbox",
            )

        raw = encode_raw(build_rfc2822(message))
        url = GMAIL_SEND_ENDPOINT.format(user_id=self.user_id)
        body = {"raw": raw}
        if message.granada_message_ref:
            # Gmail does not accept a caller-supplied idempotency token on send, so a
            # duplicate submission is guarded by Granada's own durable key rather than
            # by the provider. Recorded here because it changes what reconciliation
            # has to do.
            body["threadId"] = None  # explicitly unset; present only as documentation

        try:
            response = self.transport.request(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                    # Offered so a provider that honours it can collapse a duplicate.
                    "X-Granada-Idempotency-Key": idempotency_key,
                },
                json_body={"raw": raw},
                timeout=self.timeout,
            )
        except HttpError as exc:
            # NOBODY ANSWERED. Not a rejection.
            return _unknown(SendFailure.NETWORK_TIMEOUT, "TRANSPORT", str(exc))

        return self._interpret(response)

    # ------------------------------------------------------------------
    def _interpret(self, response: HttpResponse) -> SubmitResult:
        if response.ok:
            payload = response.json() or {}
            message_id = payload.get("id")
            if not message_id:
                # A 200 with no id is not proof of acceptance. Saying SENT here would
                # claim a receipt that does not exist.
                return _unknown(
                    SendFailure.PROVIDER_ERROR_UNKNOWN,
                    "NO_MESSAGE_ID",
                    "Gmail returned success without a message id",
                )
            return SubmitResult(
                outcome=SendOutcome.CONFIRMED_SENT,
                provider_submission_id=message_id,
                provider_message_id=message_id,
                accepted_at=datetime.now(timezone.utc),
                raw_provider_reference=message_id,
            )

        return _interpret_status(
            response,
            definite_4xx=_gmail_definite_failure,
            retry_after=_retry_after(response),
        )

    # ------------------------------------------------------------------
    def query_submission(
        self, *, granada_message_ref: str, provider_submission_id: Optional[str] = None
    ) -> ReconciliationResult:
        """Look the message up by the id Gmail gave us.

        Gmail returns a real id, so this is a direct fetch rather than a search -
        which means a positive answer is genuinely authoritative and a 404 is
        genuinely authoritative absence.
        """
        if not provider_submission_id:
            # No id: we cannot prove anything, and claiming authoritative absence
            # would unlock a retry that could duplicate the send.
            return ReconciliationResult(
                found=False,
                authoritative_absence=False,
                detail="no Gmail message id was recorded, so nothing can be looked up",
            )
        if not self.access_token:
            return ReconciliationResult(
                found=False, authoritative_absence=False,
                detail="no access token available for reconciliation",
            )

        url = GMAIL_MESSAGES_GET.format(
            user_id=self.user_id, message_id=provider_submission_id
        )
        try:
            response = self.transport.request(
                "GET", url,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=self.timeout,
            )
        except HttpError as exc:
            return ReconciliationResult(
                found=False, authoritative_absence=False,
                detail=f"the lookup failed: {exc}",
            )

        if response.ok:
            payload = response.json() or {}
            labels = payload.get("labelIds") or []
            return ReconciliationResult(
                found=True,
                outcome=SendOutcome.CONFIRMED_SENT
                if ("SENT" in labels or payload.get("id"))
                else None,
                provider_submission_id=payload.get("id") or provider_submission_id,
                provider_message_id=payload.get("id") or provider_submission_id,
                accepted_at=datetime.now(timezone.utc),
                # A found message IS authoritative: Gmail answered about a specific id.
                authoritative_absence=False,
                detail="Gmail returned the message",
            )
        if response.status == 404:
            return ReconciliationResult(
                found=False,
                authoritative_absence=True,
                detail="Gmail authoritatively reports no such message id",
            )
        return ReconciliationResult(
            found=False, authoritative_absence=False,
            detail=f"the lookup returned HTTP {response.status}",
        )


# ---------------------------------------------------------------------------
# Shared status interpretation
# ---------------------------------------------------------------------------
def _retry_after(response: HttpResponse) -> Optional[int]:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _unknown(failure: SendFailure, code: str, summary: str) -> SubmitResult:
    return SubmitResult(
        outcome=SendOutcome.DELIVERY_UNKNOWN,
        failure=failure,
        error_code=code,
        safe_error_summary=summary[:300],
    )


def _gmail_definite_failure(status: int, payload: Any) -> Optional[SendFailure]:
    if status in (401, 403):
        return SendFailure.AUTH_REQUIRED
    if status == 429:
        return SendFailure.RATE_LIMITED
    if status == 413:
        return SendFailure.INVALID_MESSAGE
    error = (payload or {}).get("error") if isinstance(payload, dict) else None
    reason = ""
    if isinstance(error, dict):
        reason = str(error.get("status") or "")
        for detail in error.get("errors") or []:
            reason += " " + str(detail.get("reason") or "")
    if "invalidArgument" in reason or "invalid" in reason.lower():
        return SendFailure.INVALID_MESSAGE
    if "forbidden" in reason.lower() or "policy" in reason.lower():
        return SendFailure.PROVIDER_POLICY_BLOCK
    return None


def _interpret_status(
    response: HttpResponse,
    *,
    definite_4xx: Any,
    retry_after: Optional[int],
) -> SubmitResult:
    """Map a failing HTTP response onto a definite failure or an unknown outcome.

    **The 5xx case is the one that matters.** A 500 says the provider answered with
    an error, not that it declined the message - a request can be accepted and then
    fail while composing the response. So 5xx is ``DELIVERY_UNKNOWN``, and retrying
    it blindly would be exactly the duplicate the phase exists to prevent.
    """
    status = response.status
    payload = response.json()

    if 400 <= status < 500:
        failure = definite_4xx(status, payload)
        if failure is not None:
            return SubmitResult(
                outcome=SendOutcome.CONFIRMED_NOT_SENT,
                failure=failure,
                error_code=f"HTTP_{status}",
                safe_error_summary=response.text,
                retry_after_seconds=retry_after,
            )
        # An unrecognised 4xx. The provider answered, but Granada cannot say the
        # message was not accepted, so it must not claim it was.
        return _unknown(
            SendFailure.PROVIDER_ERROR_UNKNOWN,
            f"HTTP_{status}",
            f"unrecognised client error: {response.text}",
        )

    return _unknown(
        SendFailure.PROVIDER_ERROR_UNKNOWN,
        f"HTTP_{status}",
        "the provider returned a server error, which does not prove the message was "
        "not accepted",
    )
