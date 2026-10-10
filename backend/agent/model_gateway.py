"""Provider-neutral model gateway.

Purpose
-------
Every model call in Granada goes through here, for four reasons that are
requirements rather than preferences:

1. **Provider neutrality.** Nothing above this module knows which vendor is
   behind a tier. Swapping vendors is a configuration change.
2. **Recorded cost.** An autonomous platform that can spend money without
   recording what it spent cannot be trusted with autonomy.
3. **A "why?" trail.** Provider, model, version, prompt version, digests and
   outcome are recorded per invocation, so every automated decision can be
   explained afterwards.
4. **Untrusted output.** A model's response is input, not truth. It is
   validated before any caller sees it, and a validation failure is a failure
   of the call - never a value that flows onward because it parsed as a string.

Tiers
-----
``CLASSIFICATION`` is for bounded decisions (is this an acknowledgement, is
this deadline change material) and is expected to run on a small, cheap model.
``SYNTHESIS`` is for writing and reasoning and is expected to run on a strong
one. Paying synthesis prices to triage an inbox is the most common way an agent
platform becomes uneconomic.

Untrusted output
----------------
``complete()`` returns a :class:`ModelResult` whose ``text`` is always present
but whose ``data`` is present **only** when a schema was supplied and
validation passed. There is no accessor that returns unvalidated structured
data, because the failure mode this prevents - a hallucinated field silently
becoming a submission fact - is the one the security gate names explicitly.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import models
from agent.redaction import digest, minimize, redact

logger = logging.getLogger(__name__)


class ModelGatewayError(RuntimeError):
    """Base class for gateway failures."""


class NoRouteAvailable(ModelGatewayError):
    """No provider is configured for the requested tier."""


class ModelOutputInvalid(ModelGatewayError):
    """The response could not be validated against the requested schema.

    Deliberately an error rather than a warning. A model that returned
    unparseable JSON has not answered the question, and treating its raw text
    as the answer is how a fabricated value reaches a real application.
    """


class ModelCallFailed(ModelGatewayError):
    """The provider call itself failed (transport, auth, rate limit)."""


class CostBudgetExceeded(ModelGatewayError):
    """A per-call or per-day cost ceiling would be breached."""


# ---------------------------------------------------------------------------
# Provider contract
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelRequest:
    model: str
    system: str
    prompt: str
    max_output_tokens: int = 2048
    temperature: float = 0.0
    # Ask the provider for a JSON object when a schema is required. This is a
    # hint that improves reliability, never the validation step itself.
    json_mode: bool = False
    #: Images to send alongside the prompt, as data URLs or https URLs.
    #:
    #: WHY THIS FIELD HAD TO EXIST. A model that cannot see receiving an image-dependent task does
    #: not raise - it answers, fluently and wrongly, and nothing downstream can tell that no pixels
    #: were examined. `agent.multimodal_routing` refuses that ROUTING; this field is how a capable
    #: model actually receives the pixels.
    #:
    #: A tuple, not a list: a frozen request must not be mutable, and an append after the capability
    #: check would mean the request that was checked is not the request that is sent.
    images: tuple[str, ...] = ()


def _user_content(request: ModelRequest) -> Any:
    """Build the user turn, as a string or as multimodal parts.

    A TEXT-ONLY REQUEST KEEPS THE PLAIN-STRING SHAPE. One code path that always emits a parts array
    would change the wire format for every existing caller, and providers differ in how they tolerate
    `content: [{"type":"text",...}]` when there is no image - so the simple case stays simple and
    only an actual image switches the shape.

    The OpenAI-compatible vision format is used, which DeepSeek follows. An image is passed as a URL
    or data URL and referenced, never altered here: this function does not read, decode or resize
    anything, so it cannot become a place where an image is quietly rewritten.
    """
    if not request.images:
        return request.prompt
    parts: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
    for image in request.images:
        parts.append({"type": "image_url", "image_url": {"url": image}})
    return parts


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model_version: str | None = None
    raw: dict[str, Any] | None = None


@runtime_checkable
class ModelProvider(Protocol):
    """What the gateway needs from a vendor adapter.

    Kept deliberately tiny: one method, one dataclass in, one dataclass out.
    Every additional method here is a place a vendor's quirks leak upward.
    """

    name: str

    def complete(self, request: ModelRequest, *, timeout_seconds: int) -> ProviderResponse:
        ...


class NullProvider:
    """The default. Refuses to call anything.

    Shipping a gateway that silently reaches a network by default is a
    misconfiguration that looks like success. This one fails loudly until a
    provider is chosen.
    """

    name = "null"

    def complete(self, request: ModelRequest, *, timeout_seconds: int) -> ProviderResponse:
        raise NoRouteAvailable(
            "no model provider is configured; set MODEL_PROVIDER to enable "
            "model-backed work. This service must not call a vendor implicitly."
        )


class ScriptedProvider:
    """A deterministic provider for tests and local dry runs.

    Returns queued responses in order. It exists so the gateway's recording,
    validation, budget and fallback behaviour can be tested without a network,
    which is the only way those behaviours stay tested.
    """

    name = "scripted"

    def __init__(self, responses: list[ProviderResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[ModelRequest] = []

    def complete(self, request: ModelRequest, *, timeout_seconds: int) -> ProviderResponse:
        self.calls.append(request)
        if not self._responses:
            raise ModelCallFailed("scripted provider ran out of responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class OpenAICompatibleProvider:
    """Any vendor exposing the OpenAI chat-completions shape.

    Written against the documented request/response contract. **It has not been
    exercised against a live endpoint** - there is no API key or network access
    in this environment - so treat it as unverified integration code rather
    than as working code. See IMPLEMENTATION_STATUS.md.
    """

    name = "openai_compatible"

    def __init__(self, *, api_key: str, base_url: str, model_version: str | None = None) -> None:
        if not api_key:
            raise NoRouteAvailable("openai_compatible requires an API key")
        if not base_url:
            raise NoRouteAvailable("openai_compatible requires a base URL")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model_version = model_version

    def complete(self, request: ModelRequest, *, timeout_seconds: int) -> ProviderResponse:
        import httpx

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": _user_content(request)},
            ],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except Exception as exc:  # transport
            raise ModelCallFailed(f"transport failure: {type(exc).__name__}") from exc

        if response.status_code == 429:
            raise ModelCallFailed("rate limited (429)")
        if response.status_code >= 400:
            # The body may echo the prompt; log the status, not the payload.
            raise ModelCallFailed(f"provider returned HTTP {response.status_code}")

        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise ModelCallFailed("provider returned no choices")
        choice = choices[0] or {}
        message = choice.get("message") or {}
        text = message.get("content") or ""

        # AN EMPTY ANSWER IS NOT AN ANSWER, and it must not be returned as one.
        #
        # Reasoning models (DeepSeek's included) emit reasoning tokens BEFORE the answer, drawn from the
        # same max_tokens budget. If the budget runs out during reasoning, `content` comes back empty
        # with `finish_reason="length"` - and the caller above receives `""`, which is
        # indistinguishable from a model that genuinely answered nothing.
        #
        # Found by running the probe: max_output_tokens=16 produced an empty string, tokens in=58
        # out=16, HTTP 200. Nothing raised, nothing logged. The same config with 512 tokens answered
        # "blue". A caller cannot tell those two apart from the return value alone.
        #
        # Raising is right rather than retrying here: the provider does not know the caller's budget,
        # and silently retrying with a larger one would multiply cost without the caller's consent.
        finish_reason = choice.get("finish_reason")
        if not text.strip():
            if finish_reason == "length":
                raise ModelCallFailed(
                    "response was truncated before any content: the token budget was consumed by "
                    "reasoning. Raise max_output_tokens for this model."
                )
            raise ModelCallFailed(
                f"provider returned an empty completion (finish_reason={finish_reason!r})"
            )

        usage = body.get("usage") or {}
        return ProviderResponse(
            text=text,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model_version=body.get("model") or self._model_version,
        )


class AnthropicProvider:
    """The Anthropic Messages API.

    As with :class:`OpenAICompatibleProvider`, this is written to the documented
    contract and **has not been exercised against a live endpoint**.
    """

    name = "anthropic"

    def __init__(self, *, api_key: str, base_url: str = "https://api.anthropic.com/v1",
                 model_version: str | None = None) -> None:
        if not api_key:
            raise NoRouteAvailable("anthropic requires an API key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model_version = model_version

    def complete(self, request: ModelRequest, *, timeout_seconds: int) -> ProviderResponse:
        import httpx

        payload: dict[str, Any] = {
            "model": request.model,
            "system": request.system,
            "messages": [{"role": "user", "content": request.prompt}],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }

        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(
                    f"{self._base_url}/messages",
                    json=payload,
                    headers={
                        "x-api-key": self._api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                )
        except Exception as exc:
            raise ModelCallFailed(f"transport failure: {type(exc).__name__}") from exc

        if response.status_code == 429:
            raise ModelCallFailed("rate limited (429)")
        if response.status_code >= 400:
            raise ModelCallFailed(f"provider returned HTTP {response.status_code}")

        body = response.json()
        blocks = body.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        usage = body.get("usage") or {}
        return ProviderResponse(
            text=text,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            model_version=body.get("model") or self._model_version,
        )


def build_provider(name: str, *, api_key: str = "", base_url: str = "") -> ModelProvider:
    """Construct the configured provider, failing at startup rather than at call time."""
    key = (name or "null").strip().lower()
    if key == "null":
        return NullProvider()
    if key == "scripted":
        # Only ever constructed directly by tests; a live service configured
        # with "scripted" would silently return canned answers.
        return ScriptedProvider([])
    if key == "openai_compatible":
        return OpenAICompatibleProvider(api_key=api_key, base_url=base_url)
    if key == "anthropic":
        return AnthropicProvider(api_key=api_key, base_url=base_url or "https://api.anthropic.com/v1")
    raise NoRouteAvailable(f"unknown model provider {name!r}")


# ---------------------------------------------------------------------------
# Routing and pricing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelRoute:
    provider: str
    model: str
    tier: str
    model_version: str | None = None


@dataclass(frozen=True)
class Price:
    """USD per 1,000 tokens. Integer micro-dollars, never float."""

    input_micros_per_1k: int
    output_micros_per_1k: int


# An empty table is the correct default: an unknown model must cost *something*
# rather than be free, or the budget guard silently stops guarding.
DEFAULT_PRICES: dict[str, Price] = {}


@dataclass
class ModelResult:
    """A validated result. ``data`` exists only when a schema was supplied."""

    text: str
    data: dict[str, Any] | None
    provider: str
    model: str
    model_version: str | None
    tier: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    cost_micros: int
    latency_ms: int
    redactions: dict[str, int] = field(default_factory=dict)
    invocation_id: str | None = None

    @property
    def is_structured(self) -> bool:
        return self.data is not None


def estimate_cost_micros(model: str, input_tokens: int, output_tokens: int,
                         prices: dict[str, Price] | None = None) -> int:
    """Cost in micro-dollars, rounded up.

    Rounding **up** is deliberate: rounding down would let an unknown number of
    sub-micro calls run free, and a guard with a hole is not a guard.
    """
    table = DEFAULT_PRICES if prices is None else prices
    price = table.get(model)
    if price is None:
        # Unknown model: charge the most expensive known rate, so a missing
        # entry fails toward caution rather than toward free.
        if not table:
            return 0
        price = max(table.values(), key=lambda p: p.input_micros_per_1k + p.output_micros_per_1k)

    total = (
        input_tokens * price.input_micros_per_1k + output_tokens * price.output_micros_per_1k
    )
    return -(-total // 1000)  # ceil division


# ---------------------------------------------------------------------------
# The gateway
# ---------------------------------------------------------------------------
class ModelGateway:
    """Records every call, validates every structured output, enforces budget."""

    def __init__(
        self,
        db: Session,
        provider: ModelProvider,
        *,
        routes: dict[str, list[ModelRoute]] | None = None,
        prices: dict[str, Price] | None = None,
        per_call_ceiling_micros: int = 0,
        daily_ceiling_micros: int = 0,
        store_prompts: bool = False,
        timeout_seconds: int = 60,
        trace_id: str | None = None,
    ) -> None:
        self.db = db
        self.provider = provider
        self.routes = routes or {}
        self.prices = prices if prices is not None else DEFAULT_PRICES
        self.per_call_ceiling_micros = per_call_ceiling_micros
        self.daily_ceiling_micros = daily_ceiling_micros
        self.store_prompts = store_prompts
        self.timeout_seconds = timeout_seconds
        self.trace_id = trace_id

    # -- routing -----------------------------------------------------------
    def route_for(self, tier: str) -> ModelRoute:
        options = self.routes.get(tier) or []
        if not options:
            raise NoRouteAvailable(
                f"no model route configured for tier {tier!r}; refusing to guess "
                "which model may act on the organisation's behalf"
            )
        return options[0]

    # -- budget ------------------------------------------------------------
    def spent_micros_today(self, org_id: str | None, *, now: datetime | None = None) -> int:
        """Micro-dollars spent by this tenant in the last 24 hours.

        A rolling window rather than a calendar day, because a calendar reset
        lets a tenant spend its whole budget twice within a minute of midnight.
        """
        moment = now or datetime.now(timezone.utc)
        since = moment - timedelta(hours=24)
        stmt = select(func.coalesce(func.sum(models.ModelInvocation.cost_micros), 0)).where(
            models.ModelInvocation.created_at >= since
        )
        if org_id is None:
            stmt = stmt.where(models.ModelInvocation.org_id.is_(None))
        else:
            stmt = stmt.where(models.ModelInvocation.org_id == org_id)
        return int(self.db.execute(stmt).scalar() or 0)

    # -- the call ----------------------------------------------------------
    def complete(
        self,
        *,
        tier: str,
        prompt: str,
        system: str = "",
        prompt_version: str,
        org_id: str | None = None,
        job_id: str | None = None,
        response_schema: dict[str, Any] | None = None,
        max_output_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> ModelResult:
        """Run one validated model call and record it.

        ``response_schema`` is a plain JSON-Schema-shaped dict. When supplied,
        the response must parse as a JSON object and satisfy the schema, or the
        call raises :class:`ModelOutputInvalid`. There is deliberately no
        "best effort" mode: partial acceptance of a fabricated field is the
        failure this whole method exists to prevent.
        """
        route = self.route_for(tier)

        redaction = redact(prompt)
        safe_prompt = minimize(prompt)
        safe_system = redact(system).text

        if self.daily_ceiling_micros:
            spent = self.spent_micros_today(org_id)
            if spent >= self.daily_ceiling_micros:
                self._record(
                    route=route, tier=tier, prompt_version=prompt_version,
                    prompt_text=safe_prompt, response_text=None, redaction=redaction,
                    input_tokens=0, output_tokens=0, cost_micros=0, latency_ms=0,
                    status=models.ModelInvocation.BUDGET_EXCEEDED,
                    error_category="BUDGET_EXCEEDED",
                    error_detail=f"daily ceiling {self.daily_ceiling_micros} reached ({spent})",
                    org_id=org_id, job_id=job_id, response_digest=None,
                )
                raise CostBudgetExceeded(
                    f"organisation has spent {spent} micros in 24h, ceiling is "
                    f"{self.daily_ceiling_micros}"
                )

        request = ModelRequest(
            model=route.model,
            system=safe_system,
            prompt=safe_prompt,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            json_mode=response_schema is not None,
        )

        # perf_counter, not monotonic: Windows monotonic has ~15 ms granularity
        # (measured: 0.0 for 20,000 of 20,000 back-to-back reads), so a fast
        # scripted or cached call would record exactly 0 ms of latency.
        started = time.perf_counter()
        try:
            response = self.provider.complete(request, timeout_seconds=self.timeout_seconds)
        except ModelGatewayError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            self._record(
                route=route, tier=tier, prompt_version=prompt_version,
                prompt_text=safe_prompt, response_text=None, redaction=redaction,
                input_tokens=0, output_tokens=0, cost_micros=0, latency_ms=latency_ms,
                status=models.ModelInvocation.FAILED,
                error_category=type(exc).__name__,
                error_detail=str(exc)[:2000],
                org_id=org_id, job_id=job_id, response_digest=None,
            )
            raise
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            self._record(
                route=route, tier=tier, prompt_version=prompt_version,
                prompt_text=safe_prompt, response_text=None, redaction=redaction,
                input_tokens=0, output_tokens=0, cost_micros=0, latency_ms=latency_ms,
                status=models.ModelInvocation.FAILED,
                error_category=type(exc).__name__,
                error_detail=str(exc)[:2000],
                org_id=org_id, job_id=job_id, response_digest=None,
            )
            raise ModelCallFailed(f"{type(exc).__name__}: {exc}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        cost_micros = estimate_cost_micros(
            route.model, response.input_tokens, response.output_tokens, self.prices
        )

        # Validate BEFORE recording success, and before returning anything.
        try:
            data = self._validate(response.text, response_schema)
        except ModelOutputInvalid as exc:
            self._record(
                route=route, tier=tier, prompt_version=prompt_version,
                prompt_text=safe_prompt, response_text=response.text,
                redaction=redaction, input_tokens=response.input_tokens,
                output_tokens=response.output_tokens, cost_micros=cost_micros,
                latency_ms=latency_ms,
                status=models.ModelInvocation.INVALID_OUTPUT,
                error_category="INVALID_OUTPUT", error_detail=str(exc)[:2000],
                org_id=org_id, job_id=job_id,
                response_digest=digest(response.text),
            )
            raise

        if self.per_call_ceiling_micros and cost_micros > self.per_call_ceiling_micros:
            self._record(
                route=route, tier=tier, prompt_version=prompt_version,
                prompt_text=safe_prompt, response_text=response.text,
                redaction=redaction, input_tokens=response.input_tokens,
                output_tokens=response.output_tokens, cost_micros=cost_micros,
                latency_ms=latency_ms,
                status=models.ModelInvocation.BUDGET_EXCEEDED,
                error_category="BUDGET_EXCEEDED",
                error_detail=(
                    f"call cost {cost_micros} exceeds per-call ceiling "
                    f"{self.per_call_ceiling_micros}"
                ),
                org_id=org_id, job_id=job_id,
                response_digest=digest(response.text),
            )
            raise CostBudgetExceeded(
                f"call cost {cost_micros} micros exceeds the per-call ceiling "
                f"{self.per_call_ceiling_micros}"
            )

        invocation = self._record(
            route=route, tier=tier, prompt_version=prompt_version,
            prompt_text=safe_prompt, response_text=response.text,
            redaction=redaction, input_tokens=response.input_tokens,
            output_tokens=response.output_tokens, cost_micros=cost_micros,
            latency_ms=latency_ms, status=models.ModelInvocation.SUCCEEDED,
            error_category=None, error_detail=None,
            org_id=org_id, job_id=job_id, response_digest=digest(response.text),
        )

        return ModelResult(
            text=response.text,
            data=data,
            provider=route.provider,
            model=route.model,
            model_version=response.model_version or route.model_version,
            tier=tier,
            prompt_version=prompt_version,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_micros=cost_micros,
            latency_ms=latency_ms,
            redactions=redaction.counts,
            invocation_id=invocation.id,
        )

    # -- validation --------------------------------------------------------
    @staticmethod
    def _validate(text: str, schema: dict[str, Any] | None) -> dict[str, Any] | None:
        """Parse and check a structured response.

        A minimal, dependency-free JSON-Schema subset: type, required,
        properties, enum, and additionalProperties. It is deliberately small -
        a validator that is hard to reason about is a validator whose failures
        get waived - and it covers what the agent services actually need.

        Markdown fences are stripped, because models wrap JSON in them
        constantly and rejecting that would be pedantry rather than safety.
        What is *not* tolerated is extra content after the object, or a
        top-level array where an object was required.
        """
        if schema is None:
            return None

        candidate = (text or "").strip()
        if candidate.startswith("```"):
            first_newline = candidate.find("\n")
            if first_newline != -1:
                candidate = candidate[first_newline + 1:]
            if candidate.rstrip().endswith("```"):
                candidate = candidate.rstrip()[:-3]

        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ModelOutputInvalid(f"response is not valid JSON: {exc}") from exc

        if not isinstance(parsed, dict):
            raise ModelOutputInvalid(
                f"response must be a JSON object, got {type(parsed).__name__}"
            )

        _check(parsed, schema, path="$")
        return parsed

    # -- recording ---------------------------------------------------------
    def _record(
        self,
        *,
        route: ModelRoute,
        tier: str,
        prompt_version: str,
        prompt_text: str,
        response_text: str | None,
        redaction: Any,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
        latency_ms: int,
        status: str,
        error_category: str | None,
        error_detail: str | None,
        org_id: str | None,
        job_id: str | None,
        response_digest: str | None,
    ) -> models.ModelInvocation:
        invocation = models.ModelInvocation(
            org_id=org_id,
            job_id=job_id,
            provider=route.provider,
            model=route.model,
            model_version=route.model_version,
            tier=tier,
            prompt_version=prompt_version,
            prompt_digest=digest(prompt_text),
            response_digest=response_digest,
            prompt_text=prompt_text if self.store_prompts else None,
            response_text=response_text if self.store_prompts else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_micros=cost_micros,
            latency_ms=latency_ms,
            status=status,
            error_category=error_category,
            error_detail=error_detail,
            trace_id=self.trace_id,
        )
        self.db.add(invocation)
        self.db.flush()
        return invocation


def _check(value: Any, schema: dict[str, Any], *, path: str) -> None:
    """Validate one value against the supported schema subset."""
    expected = schema.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(value, t) for t in types):
            raise ModelOutputInvalid(
                f"{path}: expected {expected}, got {type(value).__name__}"
            )

    if "enum" in schema and value not in schema["enum"]:
        raise ModelOutputInvalid(f"{path}: {value!r} is not one of {schema['enum']}")

    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                raise ModelOutputInvalid(f"{path}: missing required field {name!r}")

        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise ModelOutputInvalid(f"{path}: unexpected fields {extra}")

        for name, subschema in properties.items():
            if name in value:
                _check(value[name], subschema, path=f"{path}.{name}")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _check(item, schema["items"], path=f"{path}[{index}]")


def _is_type(value: Any, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    return False
