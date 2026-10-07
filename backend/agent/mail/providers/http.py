"""A tiny HTTP seam, so provider adapters are testable without a network.

The brief says "Do not guess APIs. Use current official provider documentation."
That is only half of it: an adapter written from documentation and never executed
is still a guess. This module is what lets the documented request be asserted -
URL, method, headers, exact JSON body - against a recorded expectation, with no
credentials and no network.

Deliberately narrow. Four methods and a tuple return value, because a richer client
abstraction would start to encode provider behaviour in the wrong layer, and the
whole point of `OutboundMailProvider` is that provider behaviour lives in the
adapter.
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Default request timeout. A send that hangs holds a worker, and a slow answer is
#: indistinguishable from a lost one - which is exactly the DELIVERY_UNKNOWN case.
DEFAULT_TIMEOUT_SECONDS = 30.0


class HttpError(RuntimeError):
    """The transport itself failed: DNS, TLS, timeout, connection reset.

    Distinct from an HTTP response with a failure status. A 500 means the provider
    answered; this means nobody did, and the two map to different outcomes.
    """


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return _json.loads(self.body.decode("utf-8"))
        except Exception:  # noqa: BLE001 - a non-JSON body is a provider quirk
            return None

    @property
    def text(self) -> str:
        # Truncated: a provider error body can echo the message, and this text ends
        # up in a persisted error column that is read far more casually than content.
        return self.body.decode("utf-8", errors="replace")[:500]


@runtime_checkable
class HttpTransport(Protocol):
    """What a provider adapter needs from an HTTP client."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        json_body: Optional[dict[str, Any]] = None,
        content: Optional[bytes] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> HttpResponse:
        ...


class HttpxTransport:
    """The real transport, used in production.

    Imported lazily so that a deployment without httpx can still import the module
    and fail only when it actually tries to send - which is the same discipline the
    Jev SDK follows.
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        json_body: Optional[dict[str, Any]] = None,
        content: Optional[bytes] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> HttpResponse:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise HttpError(f"httpx is required to reach the provider: {exc}") from exc

        try:
            with httpx.Client(timeout=timeout or self.timeout) as client:
                response = client.request(
                    method, url, headers=headers or {}, json=json_body, content=content
                )
        except Exception as exc:  # noqa: BLE001
            # Every transport-level failure is raised, never returned, so an adapter
            # cannot mistake "nobody answered" for "the provider said no".
            raise HttpError(f"{type(exc).__name__}: {exc}") from exc

        return HttpResponse(
            status=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.content,
        )


class RecordingHttpTransport:
    """A transport that records requests and replays scripted responses.

    The recording is what makes the adapter tests meaningful: they assert the exact
    URL, the exact headers and the exact body the official documentation specifies,
    rather than only that the adapter returned the right outcome.
    """

    def __init__(self, responses: Optional[list[HttpResponse]] = None) -> None:
        self.responses: list[HttpResponse] = list(responses or [])
        self.requests: list[dict[str, Any]] = []
        #: When set, every request raises this instead of returning - the timeout and
        #: connection-reset case, which must never be read as a rejection.
        self.raise_on_request: Optional[Exception] = None

    def queue(self, response: HttpResponse) -> "RecordingHttpTransport":
        self.responses.append(response)
        return self

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        json_body: Optional[dict[str, Any]] = None,
        content: Optional[bytes] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> HttpResponse:
        self.requests.append({
            "method": method,
            "url": url,
            "headers": dict(headers or {}),
            "json": json_body,
            "content": content,
            "timeout": timeout,
        })
        if self.raise_on_request is not None:
            raise self.raise_on_request
        if not self.responses:
            raise HttpError("no scripted response available")
        return self.responses.pop(0)

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]

    @property
    def call_count(self) -> int:
        return len(self.requests)


def json_response(status: int, payload: Any, **headers: str) -> HttpResponse:
    import json as _j

    return HttpResponse(
        status=status,
        headers={k.lower(): v for k, v in headers.items()},
        body=_j.dumps(payload).encode("utf-8"),
    )


def empty_response(status: int, **headers: str) -> HttpResponse:
    """An empty body. Microsoft Graph returns exactly this for a successful send."""
    return HttpResponse(
        status=status, headers={k.lower(): v for k, v in headers.items()}, body=b""
    )
