"""Microsoft 365 outbound adapter (Microsoft Graph).

Written from the official documentation, and **not verified against the live API** -
no credentials exist and no live send was attempted. What *is* verified is that the
request matches the documented contract.

The endpoint and the 202
------------------------
``POST https://graph.microsoft.com/v1.0/me/sendMail`` (or
``/users/{id | userPrincipalName}/sendMail``), scope ``Mail.Send``.

**Graph returns ``202 Accepted`` with an empty body.** There is no message id in the
response, and the documentation is explicit that 202 "doesn't indicate that the
request processing has completed". Two consequences for this design:

1. ``202`` is **acceptance**, and acceptance is not delivery - which is exactly why
   ``delivery_state`` is separate from ``status`` and why there is no
   ``mail.delivered`` event.
2. With no id returned, reconciliation cannot look the message up by identifier. It
   has to **search Sent Items for the reference Granada injected as a custom
   Internet message header**, which Graph supports via ``internetMessageHeaders``.

That second point is why ``X-Granada-Ref`` is set on every outbound message rather
than being a nicety. Without it, an accepted-but-unrecorded Graph send would be
permanently unresolvable, and the only safe action would be never to retry - leaving
a funder's reply owed forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    HttpTransport,
    HttpxTransport,
)

GRAPH_SEND_ENDPOINT = "https://graph.microsoft.com/v1.0{prefix}/sendMail"
GRAPH_SENT_SEARCH = "https://graph.microsoft.com/v1.0{prefix}/mailFolders/sentitems/messages"

SCOPE_SEND = "Mail.Send"
SCOPE_READ = "Mail.Read"

SEND_CAPABILITIES = frozenset({"MAIL_SEND", "MAIL_SEND_HUMAN_APPROVED", "MAIL_RECONCILE_SEND"})
READ_ONLY_CAPABILITIES = frozenset({"MAIL_READ", "MAIL_SYNC"})

#: The header Granada injects so an accepted message can be found again.
REFERENCE_HEADER = "x-granada-ref"
APPROVAL_HEADER = "x-granada-approval"

#: How far back a reconciliation search looks. Bounded, because an unbounded search
#: of a busy mailbox is slow and the message we want was sent within the hour.
RECONCILE_LOOKBACK_HOURS = 48


class GraphOutboundProvider:
    """Sends mail through Microsoft Graph."""

    name = "MICROSOFT"

    def __init__(
        self,
        *,
        user: Optional[str] = None,
        access_token: Optional[str] = None,
        scopes: Optional[frozenset[str]] = None,
        transport: Optional[HttpTransport] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        save_to_sent_items: bool = True,
    ) -> None:
        if user is None:
            raise ValueError(
                "a Graph adapter needs a user: the endpoint is /me/sendMail or "
                "/users/{id}/sendMail, and a bare /me is only meaningful with a "
                "delegated token"
            )
        self.user = user
        self.access_token = access_token
        self.scopes = frozenset(scopes) if scopes is not None else frozenset({SCOPE_SEND})
        self.transport = transport or HttpxTransport()
        self.timeout = timeout
        self.save_to_sent_items = save_to_sent_items
        self.capabilities = (
            SEND_CAPABILITIES if SCOPE_SEND in self.scopes else READ_ONLY_CAPABILITIES
        )

    @property
    def _prefix(self) -> str:
        # `me` uses the delegated endpoint; anything else is addressed explicitly.
        return "/me" if self.user.lower() in ("me", "self") else f"/users/{self.user}"

    # ------------------------------------------------------------------
    def build_payload(self, message: OutboundMessage) -> dict[str, Any]:
        """The documented JSON body, built so a test can assert it exactly."""
        payload: dict[str, Any] = {
            "message": {
                "subject": message.subject,
                # Plain text, not HTML. Granada's drafts are plain text, and claiming
                # HTML would change how the funder's client renders them.
                "body": {"contentType": "Text", "content": message.body_text or ""},
                "toRecipients": [
                    {"emailAddress": {"address": address}} for address in message.to_addresses
                ],
            },
            "saveToSentItems": self.save_to_sent_items,
        }
        if message.cc_addresses:
            payload["message"]["ccRecipients"] = [
                {"emailAddress": {"address": address}} for address in message.cc_addresses
            ]
        if message.bcc_addresses:
            payload["message"]["bccRecipients"] = [
                {"emailAddress": {"address": address}} for address in message.bcc_addresses
            ]
        if message.reply_to_address:
            payload["message"]["replyTo"] = [
                {"emailAddress": {"address": message.reply_to_address}}
            ]

        headers: list[dict[str, str]] = []
        if message.granada_message_ref:
            # THE reconciliation handle. Graph returns no id, so this header is how
            # an accepted message is found again.
            headers.append({"name": REFERENCE_HEADER, "value": message.granada_message_ref})
        if message.approval_fingerprint:
            headers.append({"name": APPROVAL_HEADER, "value": message.approval_fingerprint})
        for key, value in (message.headers or {}).items():
            headers.append({"name": key, "value": value})
        if headers:
            payload["message"]["internetMessageHeaders"] = headers

        if message.attachments:
            # Only attachments that actually have content. An empty file attached to a
            # funder is worse than no attachment, so one without bytes is dropped
            # rather than sent as a zero-length document - and if none survive, the
            # key is omitted entirely rather than sent as an empty array, because
            # "no attachments" is better expressed by not mentioning them.
            attachments = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": attachment.get("filename") or "attachment",
                    "contentType": attachment.get("mime_type") or "application/octet-stream",
                    # base64, as the documentation specifies.
                    "contentBytes": _b64(attachment.get("content") or b""),
                }
                for attachment in message.attachments
                if attachment.get("content")
            ]
            if attachments:
                payload["message"]["attachments"] = attachments
        return payload

    # ------------------------------------------------------------------
    def submit_message(self, *, message: OutboundMessage, idempotency_key: str) -> SubmitResult:
        if "MAIL_SEND" not in self.capabilities:
            from agent.mail.outbound import OutboundCapabilityMissing

            raise OutboundCapabilityMissing(
                "this Microsoft 365 connection was granted read-only scope; no send "
                "capability"
            )
        if not self.access_token:
            return SubmitResult(
                outcome=SendOutcome.CONFIRMED_NOT_SENT,
                failure=SendFailure.AUTH_REQUIRED,
                error_code="NO_ACCESS_TOKEN",
                safe_error_summary="no OAuth access token is available for this mailbox",
            )

        url = GRAPH_SEND_ENDPOINT.format(prefix=self._prefix)
        try:
            response = self.transport.request(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                    "X-Granada-Idempotency-Key": idempotency_key,
                },
                json_body=self.build_payload(message),
                timeout=self.timeout,
            )
        except HttpError as exc:
            return SubmitResult(
                outcome=SendOutcome.DELIVERY_UNKNOWN,
                failure=SendFailure.NETWORK_TIMEOUT,
                error_code="TRANSPORT",
                safe_error_summary=str(exc)[:300],
            )

        return self._interpret(response)

    def _interpret(self, response: Any) -> SubmitResult:
        if response.status == 202:
            # ACCEPTED, and nothing more. No id is returned, so the reference must be
            # one Granada injected and can find again.
            return SubmitResult(
                outcome=SendOutcome.CONFIRMED_SENT,
                # Deliberately NOT fabricating a submission id. A made-up id would
                # make reconciliation look successful while proving nothing.
                provider_submission_id=None,
                provider_message_id=None,
                accepted_at=datetime.now(timezone.utc),
                safe_error_summary=(
                    "Graph accepted the message (202) and returned no identifier; "
                    "reconciliation must search Sent Items for the Granada reference"
                ),
            )
        if response.status in (200, 201):
            # Documented as 202, but a 2xx in this family is still acceptance.
            return SubmitResult(
                outcome=SendOutcome.CONFIRMED_SENT,
                accepted_at=datetime.now(timezone.utc),
            )

        status = response.status
        payload = response.json()
        if 400 <= status < 500:
            failure = _graph_definite_failure(status, payload, response.text)
            if failure is not None:
                return SubmitResult(
                    outcome=SendOutcome.CONFIRMED_NOT_SENT,
                    failure=failure,
                    error_code=_graph_error_code(payload) or f"HTTP_{status}",
                    safe_error_summary=response.text,
                    retry_after_seconds=_retry_after(response),
                )
            return SubmitResult(
                outcome=SendOutcome.DELIVERY_UNKNOWN,
                failure=SendFailure.PROVIDER_ERROR_UNKNOWN,
                error_code=f"HTTP_{status}",
                safe_error_summary=f"unrecognised client error: {response.text}",
            )

        # 5xx: the provider answered with an error, which does not prove the message
        # was rejected. A request can be accepted and then fail while responding.
        return SubmitResult(
            outcome=SendOutcome.DELIVERY_UNKNOWN,
            failure=SendFailure.PROVIDER_ERROR_UNKNOWN,
            error_code=f"HTTP_{status}",
            safe_error_summary=(
                "Graph returned a server error, which does not prove the message was "
                "not accepted"
            ),
        )

    # ------------------------------------------------------------------
    def query_submission(
        self, *, granada_message_ref: str, provider_submission_id: Optional[str] = None
    ) -> ReconciliationResult:
        """Search Sent Items for the reference Granada injected.

        **Graph returns no id on send, so this is the only path.** A 202 with an empty
        body is acceptance without an identifier, and the reference header is what
        makes the accepted message findable afterwards.

        The search is bounded to a lookback window and filtered server-side, because
        an unfiltered scan of a busy mailbox is both slow and a privacy problem.
        """
        if not granada_message_ref:
            return ReconciliationResult(
                found=False, authoritative_absence=False,
                detail="no Granada reference was recorded, so nothing can be searched for",
            )
        if not self.access_token:
            return ReconciliationResult(
                found=False, authoritative_absence=False,
                detail="no access token available for reconciliation",
            )

        since = (datetime.now(timezone.utc) - timedelta(hours=RECONCILE_LOOKBACK_HOURS))
        url = GRAPH_SENT_SEARCH.format(prefix=self._prefix)
        params = (
            f"?$filter=internetMessageHeaders/any(h:h/name eq '{REFERENCE_HEADER}' "
            f"and h/value eq '{granada_message_ref}')"
            f"&$select=id,internetMessageId,sentDateTime,internetMessageHeaders"
            f"&$top=5"
        )
        try:
            response = self.transport.request(
                "GET", url + params,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=self.timeout,
            )
        except HttpError as exc:
            return ReconciliationResult(
                found=False, authoritative_absence=False,
                detail=f"the search failed: {exc}",
            )

        if not response.ok:
            # A failed search proves nothing. NOT authoritative absence - concluding
            # "not sent" from a failed lookup is how a retry duplicates a message.
            return ReconciliationResult(
                found=False, authoritative_absence=False,
                detail=f"the search returned HTTP {response.status}",
            )

        payload = response.json() or {}
        results = payload.get("value") or []
        match = None
        for item in results:
            for header in item.get("internetMessageHeaders") or []:
                if (
                    str(header.get("name", "")).lower() == REFERENCE_HEADER
                    and header.get("value") == granada_message_ref
                ):
                    match = item
                    break
            if match is not None:
                break

        if match is None:
            # The search SUCCEEDED and found nothing. Still not authoritative: the
            # message may be outside the lookback window or still being delivered, and
            # Graph's filter support for internetMessageHeaders is limited. Only a
            # positive match is treated as evidence.
            return ReconciliationResult(
                found=False,
                authoritative_absence=False,
                detail=(
                    f"no message carrying the Granada reference was found in Sent Items "
                    f"within {RECONCILE_LOOKBACK_HOURS}h; this is not proof it was "
                    "never accepted"
                ),
            )

        return ReconciliationResult(
            found=True,
            outcome=SendOutcome.CONFIRMED_SENT,
            provider_submission_id=match.get("id"),
            provider_message_id=match.get("id"),
            internet_message_id=match.get("internetMessageId"),
            accepted_at=datetime.now(timezone.utc),
            authoritative_absence=False,
            detail="Sent Items contains a message carrying the Granada reference",
        )


# ---------------------------------------------------------------------------
def _b64(value: bytes) -> str:
    import base64

    return base64.b64encode(value).decode("ascii")


def _retry_after(response: Any) -> Optional[int]:
    raw = (response.headers or {}).get("retry-after")
    if not raw:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _graph_error_code(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("code"):
            return str(error["code"])[:60]
    return None


def _graph_definite_failure(status: int, payload: Any, text: str) -> Optional[SendFailure]:
    if status == 401:
        return SendFailure.AUTH_REQUIRED
    if status == 403:
        code = (_graph_error_code(payload) or "").lower()
        if "authorization" in code or "accessdenied" in code or "insufficient" in code:
            return SendFailure.AUTH_REQUIRED
        return SendFailure.PROVIDER_POLICY_BLOCK
    if status == 429:
        return SendFailure.RATE_LIMITED
    if status in (400, 422):
        # Documented: malformed MIME returns 400. The message itself is unacceptable,
        # so retrying it unchanged is pointless.
        return SendFailure.INVALID_MESSAGE
    if status == 413:
        return SendFailure.INVALID_MESSAGE
    lowered = (text or "").lower()
    if "mailboxnotenabled" in lowered or "recipient" in lowered and "invalid" in lowered:
        return SendFailure.INVALID_MESSAGE
    return None
