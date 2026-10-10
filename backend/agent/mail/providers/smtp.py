"""Outbound mail over SMTP.

WHY THIS EXISTS

The only outbound adapters were Google and Microsoft Graph, both requiring OAuth and both reachable
only through a consent screen. Meanwhile `config.py` declared `smtp_host`, `smtp_port`, `smtp_user`,
`smtp_pass`, `smtp_tls` and `smtp_ssl` - and **no code read any of them**. The settings were dead
configuration, which is worse than missing configuration: a deployment could set them, see them
accepted, and believe mail was wired.

SMTP is what "set up email" means for most mailboxes. This adapter is that path.

THE THREE FAILURE ZONES, WHICH ARE THE WHOLE POINT

SMTP is a conversation, and WHERE it breaks decides whether a retry is safe:

    1. connect / EHLO / STARTTLS / AUTH   -> nothing sent        -> CONFIRMED_NOT_SENT, retry is safe
    2. MAIL FROM / RCPT TO                -> rejected pre-data   -> CONFIRMED_NOT_SENT, retry is safe
    3. DATA (the terminating ".")         -> AMBIGUOUS           -> DELIVERY_UNKNOWN, retry is FORBIDDEN

Zone 3 is why this is written against the low-level `mail`/`rcpt`/`data` calls rather than
`smtplib.sendmail()`. `sendmail()` collapses all three into one exception, and the caller then cannot
tell "the server refused the recipient" from "the server may have delivered the message" - so it either
retries a message that was delivered, or abandons one that was not. `SubmitResult.is_definite` and
`may_retry_now` exist to carry exactly that distinction, and an adapter that cannot produce it is
unusable for donor mail.

WHY `query_submission` CANNOT LOOK ANYTHING UP

SMTP has no send-log to query. It cannot prove a message was NOT sent, so this adapter returns
`found=False` with `authoritative_absence=False` - the honest answer, and the one that FORBIDS a
retry. Claiming authoritative absence would license a duplicate send. A future IMAP-backed adapter
could search the Sent folder and answer better; this one must not pretend.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import time
from dataclasses import dataclass
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

logger = logging.getLogger(__name__)

#: What a send-capable adapter declares. The authority check reads this and refuses MAIL_SEND when the
#: capability is absent, so a read-only account cannot be talked into sending.
#:
#: `MAIL_RECONCILE_SEND` is deliberately ABSENT. Reconciliation asserts what happened to an earlier
#: attempt, and SMTP cannot do that - advertising it would let the pipeline ask a question this adapter
#: is guaranteed to answer with "unknown".
SEND_CAPABLE = frozenset({"MAIL_SEND", "MAIL_SEND_HUMAN_APPROVED"})

#: SMTP reply codes worth distinguishing. 4xx is transient, 5xx is permanent.
_TRANSIENT_MIN = 400
_PERMANENT_MIN = 500


@dataclass(frozen=True)
class SmtpConfig:
    """Everything needed to reach one mailbox. No defaults for the credential fields.

    An empty host would fail at the first send; requiring it here fails at construction instead, which
    is the difference between a service that refuses to start and one that loses a donor email.
    """

    host: str
    port: int = 587
    username: str = ""
    password: str = ""
    #: STARTTLS on a plaintext connection. The usual choice on 587.
    use_starttls: bool = True
    #: Implicit TLS from the first byte. The usual choice on 465.
    use_ssl: bool = False
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("smtp host is required")
        if self.use_ssl and self.use_starttls:
            # Both is not "more secure", it is a startup error on every real server: the session is
            # already encrypted and STARTTLS is then refused or ignored depending on the server.
            raise ValueError("use_ssl and use_starttls are mutually exclusive")
        if self.username and not self.password:
            raise ValueError("smtp_username is set but smtp_password is empty")


class SmtpOutboundMailProvider:
    """Send mail through a standard SMTP server.

    Stateless between calls: a connection per submission. A pooled connection would be faster and would
    also mean a worker that crashed mid-send leaves an open session another worker may reuse in an
    unknown state - for donor mail, one connect per message is the cheaper mistake.
    """

    name = "SMTP"

    def __init__(self, config: SmtpConfig, *, capabilities: frozenset[str] = SEND_CAPABLE) -> None:
        self.config = config
        self.capabilities = capabilities
        #: Filled on success. Read by tests and by an operator diagnosing a send.
        self.last_provider_message_id: Optional[str] = None

    # ------------------------------------------------------------------
    def submit_message(self, *, message: OutboundMessage, idempotency_key: str) -> SubmitResult:
        """Hand the message to the server, classifying failure by WHERE it happened.

        `idempotency_key` is accepted because the interface requires it and is NOT used: SMTP has no
        idempotency token. That is recorded in `capabilities` by omission rather than pretended here -
        a duplicate submission is prevented by Granada's own ledger, not by the server.
        """
        started = time.monotonic()
        built = self._build(message)

        smtp: Optional[smtplib.SMTP] = None
        try:
            smtp = self._connect()
            self._authenticate(smtp)

            # ZONE 2 - pre-data. A rejection here is definite: the server has not accepted the body.
            try:
                smtp.mail(message.from_address)
                for address in message.to_addresses + message.cc_addresses + message.bcc_addresses:
                    smtp.rcpt(address)
            except smtplib.SMTPResponseException as exc:
                return self._definite_failure(exc, latency_ms=_ms(started))

            # ZONE 3 - the body and its terminator. If this raises, the server MAY have taken the
            # message and lost the reply, so the outcome must be the ambiguous one.
            try:
                smtp.data(built.as_bytes())
            except (smtplib.SMTPException, OSError) as exc:
                logger.warning(
                    "mail.smtp_ambiguous_after_data",
                    extra={"error": type(exc).__name__, "code": getattr(exc, "smtp_code", None)},
                )
                return SubmitResult(
                    outcome=SendOutcome.DELIVERY_UNKNOWN,
                    failure=SendFailure.NETWORK_TIMEOUT,
                    error_code=f"SMTP_AFTER_DATA_{getattr(exc, 'smtp_code', 'ERROR')}",
                    safe_error_summary=(
                        "the server connection failed after the message body was sent; Granada "
                        "cannot determine whether it was accepted, so a retry is forbidden until "
                        "the message is reconciled"
                    ),
                    latency_ms=_ms(started),
                )

            # Past `data()` the server answered the terminator with a 2xx - the message is accepted.
            message_id = built["Message-ID"] or make_msgid()
            self.last_provider_message_id = message_id

            try:
                smtp.quit()
            except Exception:  # noqa: BLE001 - the message is already accepted; QUIT is courtesy
                logger.debug("mail.smtp_quit_failed", exc_info=True)

            return SubmitResult(
                outcome=SendOutcome.CONFIRMED_SENT,
                provider_submission_id=f"smtp:{message_id}",
                provider_message_id=message_id,
                internet_message_id=message_id,
                accepted_at=datetime.now(timezone.utc),
                latency_ms=_ms(started),
                raw_provider_reference=message_id,
            )

        except _AuthFailed as exc:
            return SubmitResult(
                outcome=SendOutcome.CONFIRMED_NOT_SENT,
                failure=SendFailure.AUTH_REQUIRED,
                error_code=str(exc.smtp_code or "AUTH"),
                safe_error_summary="the SMTP server rejected the credentials; nothing was sent",
                latency_ms=_ms(started),
            )
        except (smtplib.SMTPConnectError, ConnectionError, OSError) as exc:
            # Could not reach the server at all. Definite: no conversation happened.
            return SubmitResult(
                outcome=SendOutcome.CONFIRMED_NOT_SENT,
                failure=SendFailure.TEMPORARY_FAILURE,
                error_code=type(exc).__name__,
                safe_error_summary="could not connect to the SMTP server; nothing was sent",
                retry_after_seconds=60,
                latency_ms=_ms(started),
            )
        except smtplib.SMTPException as exc:
            return self._definite_failure(exc, latency_ms=_ms(started))
        finally:
            if smtp is not None:
                try:
                    smtp.close()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------
    def query_submission(
        self, *, granada_message_ref: str, provider_submission_id: Optional[str] = None
    ) -> ReconciliationResult:
        """Honest ignorance, which forbids a retry.

        SMTP keeps no send-log a client can read, so there is no way to prove a message was NOT
        delivered. `authoritative_absence=False` is what stops the pipeline treating this as
        permission to send again - the duplicate this whole module is shaped to prevent.
        """
        return ReconciliationResult(
            found=False,
            authoritative_absence=False,
            detail=(
                "SMTP provides no send log to query. Granada cannot prove the message was not "
                "delivered, so it must not be resent automatically."
            ),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _connect(self) -> smtplib.SMTP:
        config = self.config
        if config.use_ssl:
            context = ssl.create_default_context()
            return smtplib.SMTP_SSL(
                config.host, config.port, timeout=config.timeout_seconds, context=context
            )
        smtp = smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds)
        smtp.ehlo()
        if config.use_starttls:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        return smtp

    def _authenticate(self, smtp: smtplib.SMTP) -> None:
        if not self.config.username:
            return
        try:
            smtp.login(self.config.username, self.config.password)
        except smtplib.SMTPAuthenticationError as exc:
            # Distinct from every other failure: bad credentials are permanent until a human acts, and
            # the caller must not retry the same message on a timer forever.
            raise _AuthFailed(getattr(exc, "smtp_code", None)) from exc

    def _build(self, message: OutboundMessage) -> EmailMessage:
        built = EmailMessage()
        built["From"] = message.from_address
        built["To"] = ", ".join(message.to_addresses)
        if message.cc_addresses:
            built["Cc"] = ", ".join(message.cc_addresses)
        if message.reply_to_address:
            built["Reply-To"] = message.reply_to_address
        built["Subject"] = message.subject
        built["Date"] = formatdate(localtime=False)
        # The Message-ID is set explicitly when Granada supplied one, so a reconciled reply thread can
        # be matched back even when the server would have generated a different value.
        built["Message-ID"] = message.internet_message_id or make_msgid()
        # Self-describing about what was authorised: the fingerprint travels with the sent message, so
        # "the provider sent what was approved" is checkable from the recipient's copy.
        if message.approval_fingerprint:
            built["X-Granada-Approval-Fingerprint"] = message.approval_fingerprint
        for key, value in (message.headers or {}).items():
            if key.lower() in {"to", "cc", "bcc", "from", "subject", "message-id"}:
                # Refusing here rather than overwriting: a header that silently replaces a recipient
                # is how a message reaches the wrong funder.
                raise ValueError(f"header {key!r} may not be overridden")
            built[key] = value

        built.set_content(message.body_text or "")
        for attachment in message.attachments:
            content = attachment.get("content") or b""
            if isinstance(content, str):
                content = content.encode("utf-8")
            built.add_attachment(
                content,
                maintype=(attachment.get("mime_type") or "application/octet-stream").split("/")[0],
                subtype=(attachment.get("mime_type") or "application/octet-stream").split("/")[-1],
                filename=attachment.get("filename") or "attachment.bin",
            )
        return built

    def _definite_failure(self, exc: Exception, *, latency_ms: int) -> SubmitResult:
        """Map an SMTP pre-data rejection onto Granada's vocabulary."""
        code = getattr(exc, "smtp_code", 0) or 0
        failure = _failure_for_code(code)
        return SubmitResult(
            outcome=SendOutcome.CONFIRMED_NOT_SENT,
            failure=failure,
            error_code=str(code or type(exc).__name__),
            safe_error_summary=_summary_for(failure),
            retry_after_seconds=60 if failure == SendFailure.RATE_LIMITED else None,
            latency_ms=latency_ms,
        )


class _AuthFailed(smtplib.SMTPException):
    """Internal marker so authentication failures are not confused with send failures.

    Carries `smtp_code` explicitly. `SMTPException` stores positional constructor arguments in
    `.args`, NOT in `.smtp_code` - that attribute belongs to `SMTPResponseException`, and the first
    version of this class inherited the wrong one and raised AttributeError while handling an
    authentication failure, which is a particularly bad moment to raise.
    """

    def __init__(self, smtp_code: int | None = None) -> None:
        super().__init__(smtp_code)
        self.smtp_code = smtp_code


def _failure_for_code(code: int) -> SendFailure:
    """Map an SMTP reply code onto Granada's failure vocabulary.

    ONLY the members that actually exist in `SendFailure` are used. The first draft of this function
    invented `PERMANENT_FAILURE`, `AUTHENTICATION_FAILED`, `RECIPIENT_REJECTED`, `MESSAGE_REJECTED` and
    `PROVIDER_UNAVAILABLE` - every one of them plausible, none of them real. That is the defect this
    project keeps recording: an interface written from memory instead of read, which fails at import
    rather than at review.
    """
    if code in (450, 451, 452, 421):
        # 421 is "service not available" and 45x are transient; both are worth retrying later.
        return SendFailure.TEMPORARY_FAILURE
    if code in (550, 551, 553):
        # A bad mailbox or a bad sender address. Definite, and retrying the same address is pointless.
        return SendFailure.INVALID_MESSAGE
    if code == 552:
        return SendFailure.INVALID_MESSAGE  # exceeded storage allocation
    if code == 554:
        # "Transaction failed" - the server's catch-all policy refusal.
        return SendFailure.PROVIDER_POLICY_BLOCK
    if code >= _PERMANENT_MIN:
        return SendFailure.PERMANENT_REJECTION
    if code >= _TRANSIENT_MIN:
        return SendFailure.TEMPORARY_FAILURE
    # Reached only for a non-numeric or unexpected code. TEMPORARY_FAILURE is in DEFINITE_NOT_SENT, so
    # the message is still safe to retry - which is the correct reading of a pre-data failure.
    return SendFailure.TEMPORARY_FAILURE


def _summary_for(failure: SendFailure) -> str:
    # Deliberately does not echo the server's message: it can quote the recipient or the body, and this
    # string is persisted and read far more casually than message content.
    return {
        SendFailure.INVALID_MESSAGE: "the SMTP server rejected the address or message; nothing was sent",
        SendFailure.TEMPORARY_FAILURE: "the SMTP server reported a temporary failure; nothing was sent",
        SendFailure.PERMANENT_REJECTION: "the SMTP server returned a permanent failure; nothing was sent",
        SendFailure.RATE_LIMITED: "the SMTP server is rate limiting; nothing was sent",
        SendFailure.PROVIDER_POLICY_BLOCK: "the SMTP server refused on policy grounds; nothing was sent",
    }.get(failure, "the SMTP server refused the message before the body was sent")


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def build_from_settings(settings: Any) -> Optional[SmtpOutboundMailProvider]:
    """Construct from the application settings, or return None when SMTP is not configured.

    `None` rather than an exception: an unconfigured mail transport is a deployment state, and the
    caller's correct response is to park the work, not to fail the job and burn a retry budget.
    """
    host = getattr(settings, "smtp_host", "") or ""
    if not host or host == "localhost":
        # `localhost` is the declared default and means "unset" here. A real local relay is rare enough
        # that requiring an explicit value is worth not silently attempting a connection on every send.
        return None
    return SmtpOutboundMailProvider(
        SmtpConfig(
            host=host,
            port=int(getattr(settings, "smtp_port", 587) or 587),
            username=getattr(settings, "smtp_user", "") or "",
            password=getattr(settings, "smtp_pass", "") or "",
            use_starttls=bool(getattr(settings, "smtp_tls", True)),
            use_ssl=bool(getattr(settings, "smtp_ssl", False)),
        )
    )
