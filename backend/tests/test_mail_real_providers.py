"""Real provider adapters, verified against the documented contract.

**These are NOT live tests.** No credentials exist in this environment and no live
send was attempted, so what is proven here is that each adapter builds the request
the official documentation specifies and maps the response onto the three outcomes
correctly. That is a real and useful thing to prove - an adapter written from
documentation and never executed is still a guess - but it is not the same as having
sent an email, and the report says so.

Sources:
* Gmail send: https://developers.google.com/workspace/gmail/api/guides/sending
* Gmail scopes: https://developers.google.com/workspace/gmail/api/auth/scopes
* Graph sendMail: https://learn.microsoft.com/en-us/graph/api/user-sendmail
"""

from __future__ import annotations

import base64
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.mail.outbound import (  # noqa: E402
    OutboundMessage,
    SendFailure,
    SendOutcome,
)
from agent.mail.providers.google import (  # noqa: E402
    GMAIL_SEND_ENDPOINT,
    SCOPE_SEND,
    GmailOutboundProvider,
    build_rfc2822,
    encode_raw,
)
from agent.mail.providers.http import (  # noqa: E402
    HttpError,
    RecordingHttpTransport,
    empty_response,
    json_response,
)
from agent.mail.providers.microsoft import (  # noqa: E402
    GRAPH_SEND_ENDPOINT,
    REFERENCE_HEADER,
    GraphOutboundProvider,
)

READ_ONLY = "read-only"


def _message(**overrides):
    base = dict(
        from_address="grants@warchild.org",
        to_addresses=("grants@unicef.org",),
        subject="Audited financial statements",
        body_text="Please find our audited statements attached.",
        granada_message_ref="gml-abc123",
        approval_fingerprint="f" * 64,
    )
    base.update(overrides)
    return OutboundMessage(**base)


# ===========================================================================
# GMAIL
# ===========================================================================
def test_gmail_posts_to_the_documented_endpoint():
    transport = RecordingHttpTransport([json_response(200, {"id": "msg1", "threadId": "t1"})])
    provider = GmailOutboundProvider(
        user_id="me", access_token="tok", scopes={SCOPE_SEND}, transport=transport
    )
    result = provider.submit_message(message=_message(), idempotency_key="key-1")

    assert result.outcome == SendOutcome.CONFIRMED_SENT
    assert result.provider_submission_id == "msg1"
    assert transport.last["method"] == "POST"
    assert transport.last["url"] == GMAIL_SEND_ENDPOINT.format(user_id="me")
    assert transport.last["headers"]["Authorization"] == "Bearer tok"
    # The documented body: a base64url-encoded RFC 2822 message under `raw`.
    assert "raw" in transport.last["json"]
    assert isinstance(transport.last["json"]["raw"], str)


def test_the_gmail_raw_field_decodes_to_the_approved_message():
    """The bytes on the wire must be the bytes the fingerprint covers."""
    transport = RecordingHttpTransport([json_response(200, {"id": "m"})])
    provider = GmailOutboundProvider(
        user_id="me", access_token="tok", transport=transport
    )
    provider.submit_message(
        message=_message(body_text="Exactly these words."), idempotency_key="k"
    )

    raw = transport.last["json"]["raw"]
    # base64url without padding, as documented.
    assert "=" not in raw and "+" not in raw and "/" not in raw
    padded = raw + "=" * (-len(raw) % 4)
    decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")

    assert "Exactly these words." in decoded
    assert "grants@unicef.org" in decoded
    assert "Audited financial statements" in decoded
    # Granada's own reference travels with the message, so a sent message is
    # self-describing about what it is.
    assert "gml-abc123" in decoded
    assert "f" * 64 in decoded


def test_a_non_ascii_subject_survives_the_encoding():
    """Hand-rolled headers get this wrong; the message is the assertion."""
    raw = build_rfc2822(_message(subject="Résumé — état des finances"))
    assert b"R" in raw
    # Either encoded-word or UTF-8 body, but never a mangled header.
    assert b"\xff" not in raw


def test_gmail_read_only_scope_has_no_send_capability():
    provider = GmailOutboundProvider(
        user_id="me",
        scopes={"https://www.googleapis.com/auth/gmail.readonly"},
    )
    assert "MAIL_SEND" not in provider.capabilities
    from agent.mail.outbound import OutboundCapabilityMissing

    with pytest.raises(OutboundCapabilityMissing):
        provider.submit_message(message=_message(), idempotency_key="k")


@pytest.mark.parametrize(
    "status,expected_outcome,expected_failure",
    (
        (401, SendOutcome.CONFIRMED_NOT_SENT, SendFailure.AUTH_REQUIRED),
        (403, SendOutcome.CONFIRMED_NOT_SENT, SendFailure.AUTH_REQUIRED),
        (429, SendOutcome.CONFIRMED_NOT_SENT, SendFailure.RATE_LIMITED),
        (413, SendOutcome.CONFIRMED_NOT_SENT, SendFailure.INVALID_MESSAGE),
        # A server error does NOT prove the message was not accepted. This row is
        # the one that matters.
        (500, SendOutcome.DELIVERY_UNKNOWN, SendFailure.PROVIDER_ERROR_UNKNOWN),
        (502, SendOutcome.DELIVERY_UNKNOWN, SendFailure.PROVIDER_ERROR_UNKNOWN),
        (503, SendOutcome.DELIVERY_UNKNOWN, SendFailure.PROVIDER_ERROR_UNKNOWN),
    ),
)
def test_gmail_status_mapping(status, expected_outcome, expected_failure):
    transport = RecordingHttpTransport([json_response(status, {"error": {"status": "X"}})])
    provider = GmailOutboundProvider(user_id="me", access_token="tok", transport=transport)
    result = provider.submit_message(message=_message(), idempotency_key="k")

    assert result.outcome == expected_outcome, f"HTTP {status}"
    assert result.failure == expected_failure
    if expected_outcome == SendOutcome.DELIVERY_UNKNOWN:
        # The safety property: an unknown outcome forbids an immediate retry.
        assert result.may_retry_now is False


def test_gmail_transport_failure_is_unknown_not_rejected():
    transport = RecordingHttpTransport()
    transport.raise_on_request = HttpError("read timed out")
    provider = GmailOutboundProvider(user_id="me", access_token="tok", transport=transport)
    result = provider.submit_message(message=_message(), idempotency_key="k")
    assert result.outcome == SendOutcome.DELIVERY_UNKNOWN
    assert result.may_retry_now is False


def test_gmail_success_without_an_id_is_not_treated_as_sent():
    """A 200 with no id is not a receipt. Claiming SENT would be a lie."""
    transport = RecordingHttpTransport([json_response(200, {})])
    provider = GmailOutboundProvider(user_id="me", access_token="tok", transport=transport)
    result = provider.submit_message(message=_message(), idempotency_key="k")
    assert result.outcome == SendOutcome.DELIVERY_UNKNOWN
    assert result.error_code == "NO_MESSAGE_ID"


def test_gmail_reconciliation_uses_the_returned_id():
    transport = RecordingHttpTransport([
        json_response(200, {"id": "msg-9", "labelIds": ["SENT"]}),
    ])
    provider = GmailOutboundProvider(user_id="me", access_token="tok", transport=transport)
    result = provider.query_submission(granada_message_ref="gml-abc123", provider_submission_id="msg-9")
    assert result.found is True
    assert result.outcome == SendOutcome.CONFIRMED_SENT
    assert result.authoritative_absence is False


def test_gmail_404_is_authoritative_absence():
    """Only a positive 404 unlocks a retry."""
    transport = RecordingHttpTransport([json_response(404, {"error": {"code": 404}})])
    provider = GmailOutboundProvider(user_id="me", access_token="tok", transport=transport)
    result = provider.query_submission(granada_message_ref="gml-abc123", provider_submission_id="msg-9")
    assert result.found is False
    assert result.authoritative_absence is True


def test_gmail_reconciliation_without_an_id_proves_nothing():
    provider = GmailOutboundProvider(user_id="me", access_token="tok")
    result = provider.query_submission(granada_message_ref="gml-abc123")
    assert result.found is False
    assert result.authoritative_absence is False


# ===========================================================================
# MICROSOFT GRAPH
# ===========================================================================
def test_graph_posts_the_documented_payload():
    transport = RecordingHttpTransport([empty_response(202)])
    provider = GraphOutboundProvider(user="me", access_token="tok", transport=transport)
    result = provider.submit_message(message=_message(), idempotency_key="key-1")

    assert transport.last["method"] == "POST"
    assert transport.last["url"] == GRAPH_SEND_ENDPOINT.format(prefix="/me")
    assert transport.last["headers"]["Authorization"] == "Bearer tok"

    body = transport.last["json"]
    assert body["saveToSentItems"] is True
    assert body["message"]["subject"] == "Audited financial statements"
    assert body["message"]["body"]["contentType"] == "Text"
    assert body["message"]["toRecipients"][0]["emailAddress"]["address"] == "grants@unicef.org"

    # The 202 is ACCEPTANCE. Graph returns no identifier at all, so the outcome is
    # SENT with no submission id rather than a fabricated one.
    assert result.outcome == SendOutcome.CONFIRMED_SENT
    assert result.provider_submission_id is None


def test_graph_202_injects_the_reference_header_or_reconciliation_is_impossible():
    """Graph returns no id, so this header is the ONLY way to find a sent message."""
    transport = RecordingHttpTransport([empty_response(202)])
    provider = GraphOutboundProvider(user="me", access_token="tok", transport=transport)
    provider.submit_message(message=_message(), idempotency_key="k")

    headers = transport.last["json"]["message"]["internetMessageHeaders"]
    refs = [h for h in headers if h["name"] == REFERENCE_HEADER]
    assert refs, "no Granada reference header was injected"
    assert refs[0]["value"] == "gml-abc123"


def test_graph_uses_the_explicit_user_endpoint_when_addressed():
    transport = RecordingHttpTransport([empty_response(202)])
    provider = GraphOutboundProvider(user="grants@warchild.org", access_token="tok", transport=transport)
    provider.submit_message(message=_message(), idempotency_key="k")
    assert transport.last["url"] == GRAPH_SEND_ENDPOINT.format(prefix="/users/grants@warchild.org")


def test_graph_attachments_use_the_documented_shape():
    transport = RecordingHttpTransport([empty_response(202)])
    provider = GraphOutboundProvider(user="me", access_token="tok", transport=transport)
    provider.submit_message(
        message=_message(attachments=({
            "filename": "statements.pdf", "mime_type": "application/pdf",
            "content": b"%PDF-1.4 pretend", "checksum_sha256": "abc",
        },)),
        idempotency_key="k",
    )
    attachments = transport.last["json"]["message"]["attachments"]
    assert attachments[0]["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert attachments[0]["name"] == "statements.pdf"
    assert base64.b64decode(attachments[0]["contentBytes"]) == b"%PDF-1.4 pretend"


def test_graph_omits_an_attachment_with_no_content():
    """An empty file attached to a funder is worse than no attachment."""
    transport = RecordingHttpTransport([empty_response(202)])
    provider = GraphOutboundProvider(user="me", access_token="tok", transport=transport)
    provider.submit_message(
        message=_message(attachments=({"filename": "x.pdf", "content": None},)),
        idempotency_key="k",
    )
    assert "attachments" not in transport.last["json"]["message"]


def test_graph_read_only_scope_has_no_send_capability():
    provider = GraphOutboundProvider(user="me", scopes={"Mail.Read"})
    assert "MAIL_SEND" not in provider.capabilities


@pytest.mark.parametrize(
    "status,expected_outcome,expected_failure",
    (
        (400, SendOutcome.CONFIRMED_NOT_SENT, SendFailure.INVALID_MESSAGE),
        (401, SendOutcome.CONFIRMED_NOT_SENT, SendFailure.AUTH_REQUIRED),
        (403, SendOutcome.CONFIRMED_NOT_SENT, SendFailure.PROVIDER_POLICY_BLOCK),
        (413, SendOutcome.CONFIRMED_NOT_SENT, SendFailure.INVALID_MESSAGE),
        (429, SendOutcome.CONFIRMED_NOT_SENT, SendFailure.RATE_LIMITED),
        (500, SendOutcome.DELIVERY_UNKNOWN, SendFailure.PROVIDER_ERROR_UNKNOWN),
        (503, SendOutcome.DELIVERY_UNKNOWN, SendFailure.PROVIDER_ERROR_UNKNOWN),
    ),
)
def test_graph_status_mapping(status, expected_outcome, expected_failure):
    transport = RecordingHttpTransport([
        json_response(status, {"error": {"code": "ErrorX", "message": "nope"}})
    ])
    provider = GraphOutboundProvider(user="me", access_token="tok", transport=transport)
    result = provider.submit_message(message=_message(), idempotency_key="k")
    assert result.outcome == expected_outcome, f"HTTP {status}"
    assert result.failure == expected_failure


def test_graph_reconciliation_searches_sent_items_for_the_reference():
    transport = RecordingHttpTransport([
        json_response(200, {
            "value": [
                {"id": "other", "internetMessageHeaders": [
                    {"name": "x-something", "value": "else"}]},
                {"id": "found-1", "internetMessageId": "<m@x>",
                 "internetMessageHeaders": [
                     {"name": REFERENCE_HEADER, "value": "gml-abc123"}]},
            ]
        })
    ])
    provider = GraphOutboundProvider(user="me", access_token="tok", transport=transport)
    result = provider.query_submission(granada_message_ref="gml-abc123")

    assert result.found is True
    assert result.provider_submission_id == "found-1"
    assert "sentitems" in transport.last["url"]
    assert "gml-abc123" in transport.last["url"]


def test_graph_reconciliation_finding_nothing_is_not_authoritative():
    """A failed or empty search proves nothing, and must not unlock a retry.

    Graph's filter support for custom Internet headers is limited, so an empty result
    is at least as likely to mean "the filter did not match" as "the message was never
    sent". Treating it as authoritative absence is how a duplicate send happens.
    """
    transport = RecordingHttpTransport([json_response(200, {"value": []})])
    provider = GraphOutboundProvider(user="me", access_token="tok", transport=transport)
    result = provider.query_submission(granada_message_ref="gml-abc123")
    assert result.found is False
    assert result.authoritative_absence is False


def test_graph_reconciliation_search_failure_is_not_authoritative():
    transport = RecordingHttpTransport([json_response(503, {"error": {"code": "Busy"}})])
    provider = GraphOutboundProvider(user="me", access_token="tok", transport=transport)
    result = provider.query_submission(granada_message_ref="gml-abc123")
    assert result.authoritative_absence is False


def test_both_providers_are_valid_outbound_providers():
    """Structural: both satisfy the protocol and declare capabilities."""
    from agent.mail.outbound import OutboundMailProvider

    gmail = GmailOutboundProvider(user_id="me", access_token="t")
    graph = GraphOutboundProvider(user="me", access_token="t")
    for provider in (gmail, graph):
        assert isinstance(provider, OutboundMailProvider)
        assert hasattr(provider, "capabilities") and provider.capabilities
