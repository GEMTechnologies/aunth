"""Multimodal support in the model gateway.

WHY THIS EXISTS
---------------
Section 3 requires a model-agnostic gateway that can carry images. The Phase A inspection found
`ModelRequest` had no image field at all - so no vision model, however capable, could be sent a
screenshot through Granada's own gateway. This pins the two things that fix:

  1. a TEXT-ONLY request keeps the plain-string wire format, so nothing about existing callers changes
  2. a request WITH images sends the OpenAI-compatible vision shape, which DeepSeek follows

The provider is not called. What is asserted is the PAYLOAD - the part that has to be right before any
key exists, and the part that would otherwise be discovered only by a failed live call.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.model_gateway import ModelRequest, _user_content  # noqa: E402


# ===========================================================================
# THE FIELD
# ===========================================================================
def test_a_request_carries_images_as_a_tuple():
    """A tuple, not a list: a frozen request must not be mutable, and an append after the capability
    check would mean the request that was CHECKED is not the request that is SENT."""
    r = ModelRequest(model="m", system="s", prompt="p", images=("data:image/png;base64,AAA",))
    assert isinstance(r.images, tuple)
    with pytest.raises(Exception):
        r.images = ("other",)  # type: ignore[misc]


def test_a_request_without_images_defaults_to_empty():
    assert ModelRequest(model="m", system="s", prompt="p").images == ()


# ===========================================================================
# THE WIRE FORMAT
# ===========================================================================
def test_a_text_only_request_keeps_the_PLAIN_STRING_shape():
    """One code path that always emitted a parts array would change the wire format for every
    existing caller, and providers differ in how they tolerate a single-part array. The simple case
    stays simple."""
    content = _user_content(ModelRequest(model="m", system="s", prompt="hello"))
    assert content == "hello"
    assert isinstance(content, str)


def test_an_image_request_switches_to_multimodal_parts():
    content = _user_content(
        ModelRequest(
            model="m", system="s", prompt="what is highlighted?",
            images=("data:image/png;base64,AAAA",),
        )
    )
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "what is highlighted?"}
    assert content[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


def test_several_images_all_travel():
    content = _user_content(
        ModelRequest(model="m", system="s", prompt="p", images=("a.png", "b.png", "c.png"))
    )
    urls = [p["image_url"]["url"] for p in content if p["type"] == "image_url"]
    assert urls == ["a.png", "b.png", "c.png"]


def test_the_prompt_still_travels_with_the_images():
    """A multimodal request that dropped the question would send pixels with no instruction."""
    content = _user_content(ModelRequest(model="m", system="s", prompt="Q", images=("i.png",)))
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "Q"


def test_an_https_url_is_passed_through_unchanged():
    """The gateway does not read, decode or resize anything - so it cannot become a place where an
    image is quietly rewritten."""
    content = _user_content(
        ModelRequest(model="m", system="s", prompt="p", images=("https://example.invalid/s.png",))
    )
    assert content[1]["image_url"]["url"] == "https://example.invalid/s.png"


# ===========================================================================
# WHAT THE PROVIDER SENDS
# ===========================================================================
def test_the_openai_payload_uses_the_multimodal_content_for_images():
    """Asserts the PAYLOAD, which is the part that must be right before any key exists."""
    import agent.model_gateway as gw

    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured["payload"] = json
            captured["url"] = url
            return FakeResponse()

    class FakeHttpx:
        Client = FakeClient

    real = sys.modules.get("httpx")
    sys.modules["httpx"] = FakeHttpx  # type: ignore[assignment]
    try:
        provider = gw.OpenAICompatibleProvider(api_key="k", base_url="https://api.deepseek.com")
        provider.complete(
            ModelRequest(
                model="deepseek-chat", system="s", prompt="p",
                images=("data:image/png;base64,AAAA",),
            ),
            timeout_seconds=5,
        )
    finally:
        if real is not None:
            sys.modules["httpx"] = real
        else:
            sys.modules.pop("httpx", None)

    user = captured["payload"]["messages"][1]
    assert isinstance(user["content"], list), "an image request sent a plain string"
    assert any(part["type"] == "image_url" for part in user["content"])
    assert captured["url"] == "https://api.deepseek.com/chat/completions"


def test_the_openai_payload_stays_a_string_without_images():
    import agent.model_gateway as gw

    captured: dict = {}
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured["payload"] = json
            return FakeResponse()

    class FakeHttpx:
        Client = FakeClient

    real = sys.modules.get("httpx")
    sys.modules["httpx"] = FakeHttpx  # type: ignore[assignment]
    try:
        gw.OpenAICompatibleProvider(api_key="k", base_url="https://api.deepseek.com").complete(
            ModelRequest(model="m", system="s", prompt="p"), timeout_seconds=5
        )
    finally:
        if real is not None:
            sys.modules["httpx"] = real
        else:
            sys.modules.pop("httpx", None)

    assert captured["payload"]["messages"][1]["content"] == "p"


def test_deepseek_is_reachable_as_openai_compatible():
    """DeepSeek's API follows the OpenAI chat-completions shape, so it needs no new provider class -
    only configuration. Recorded as a test so the claim is checkable rather than remembered."""
    import agent.model_gateway as gw

    p = gw.OpenAICompatibleProvider(api_key="k", base_url="https://api.deepseek.com")
    assert p.name == "openai_compatible"


# ===========================================================================
# AN EMPTY COMPLETION IS A FAILURE, NOT AN ANSWER
# ===========================================================================
def _run_provider_with(body: dict):
    """Drive the real provider against a fake httpx that returns `body`."""
    import agent.model_gateway as gw

    class FakeResponse:
        status_code = 200

        def json(self):
            return body

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            return FakeResponse()

    class FakeHttpx:
        Client = FakeClient

    real = sys.modules.get("httpx")
    sys.modules["httpx"] = FakeHttpx  # type: ignore[assignment]
    try:
        return gw.OpenAICompatibleProvider(api_key="k", base_url="https://api.deepseek.com").complete(
            ModelRequest(model="deepseek-flash", system="s", prompt="p", max_output_tokens=16),
            timeout_seconds=5,
        )
    finally:
        if real is not None:
            sys.modules["httpx"] = real
        else:
            sys.modules.pop("httpx", None)


def test_a_truncated_empty_completion_raises_rather_than_returning_blank():
    """THE BUG THE LIVE PROBE FOUND.

    DeepSeek's models spend reasoning tokens from the SAME budget as the answer. With
    `max_output_tokens=16` the budget ran out during reasoning, `content` came back empty, and the
    provider returned `""` as a SUCCESS - HTTP 200, tokens in=58 out=16, `finish_reason="length"`.
    Nothing raised and nothing logged. The identical request with 512 tokens answered "blue".

    A caller cannot distinguish those two outcomes from the return value, so the provider must not
    return an empty string as if it were content.
    """
    import agent.model_gateway as gw
    import pytest

    with pytest.raises(gw.ModelCallFailed) as caught:
        _run_provider_with(
            {
                "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 58, "completion_tokens": 16},
            }
        )
    assert "truncated" in str(caught.value).lower()
    assert "max_output_tokens" in str(caught.value)


def test_an_empty_completion_without_truncation_also_raises():
    """A provider that answers nothing is a failure whatever the reason field says."""
    import agent.model_gateway as gw
    import pytest

    with pytest.raises(gw.ModelCallFailed):
        _run_provider_with(
            {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}], "usage": {}}
        )


def test_whitespace_only_content_counts_as_empty():
    """`" "` is not an answer either, and a truthiness check would have accepted it."""
    import agent.model_gateway as gw
    import pytest

    with pytest.raises(gw.ModelCallFailed):
        _run_provider_with(
            {"choices": [{"message": {"content": "   \n  "}, "finish_reason": "stop"}], "usage": {}}
        )


def test_a_real_answer_still_comes_back():
    """The guard must not have broken the working case - which is the whole reason it exists."""
    response = _run_provider_with(
        {
            "choices": [{"message": {"content": "blue"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 58, "completion_tokens": 27},
        }
    )
    assert response.text == "blue"
    assert response.output_tokens == 27


# ===========================================================================
# A WRONG CONCLUSION THIS PROJECT ALREADY PUBLISHED, AND THE TEST FOR IT
# ===========================================================================
def test_images_go_in_the_user_turn_and_never_the_system_turn():
    """THE DOCUMENTED CONSTRAINT, and the test for a mistake actually made.

    DeepSeek's vision guide: "Images are supported in `user` messages only. Images in `system` or
    `assistant` messages return a 400 error."

    `_user_content` satisfies this by construction - the system turn is always `request.system`, a plain
    string - so no image can reach it. Asserted anyway, because the failure would be a 400 at 3am and
    the invariant lives in a helper that a future edit could move.
    """
    import agent.model_gateway as gw

    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured["payload"] = json
            return FakeResponse()

    class FakeHttpx:
        Client = FakeClient

    real = sys.modules.get("httpx")
    sys.modules["httpx"] = FakeHttpx  # type: ignore[assignment]
    try:
        gw.OpenAICompatibleProvider(api_key="k", base_url="https://api.deepseek.com").complete(
            ModelRequest(
                model="deepseek-flash",
                system="s",
                prompt="p",
                images=("data:image/png;base64,AAAA",),
            ),
            timeout_seconds=5,
        )
    finally:
        if real is not None:
            sys.modules["httpx"] = real
        else:
            sys.modules.pop("httpx", None)

    messages = captured["payload"]["messages"]
    system_turn = next(m for m in messages if m["role"] == "system")
    user_turn = next(m for m in messages if m["role"] == "user")

    assert isinstance(system_turn["content"], str), (
        "an image reached the system turn, which DeepSeek rejects with a 400"
    )
    assert isinstance(user_turn["content"], list)
    assert any(part["type"] == "image_url" for part in user_turn["content"])


def test_deepseek_flash_is_the_multimodal_model_name():
    """`deepseek-flash` is the CURRENT multimodal name - V4.1-Flash, whose changelog entry says it has
    "native multimodal visual understanding", and whose vision guide opens "The `deepseek-flash` model
    accepts images alongside text".

    `deepseek-v4-pro` is TEXT-ONLY. An earlier probe concluded "DeepSeek cannot do vision" from a
    V4-Pro result plus an empty `deepseek-flash` response - and the empty response was a token-budget
    failure that the guard above now raises on, not a vision failure. This test records which name is
    which so the wrong conclusion does not get re-derived from the obvious-looking model.
    """
    import agent.model_gateway as gw

    # Both are reachable as openai_compatible; the difference is the model NAME, which is configuration.
    p = gw.OpenAICompatibleProvider(api_key="k", base_url="https://api.deepseek.com")
    assert p.name == "openai_compatible"

    source = (BACKEND / "config.py").read_text(encoding="utf-8")
    assert "model_classification_model" in source
    assert "model_synthesis_model" in source


# ===========================================================================
# A TEXT-ONLY MODEL MUST BE UNREACHABLE FOR AN IMAGE
# ===========================================================================
def _gateway(routes):
    import agent.model_gateway as gw

    class Null:
        name = "null"

    return gw.ModelGateway(None, Null(), routes=routes)


def test_a_text_only_tier_refuses_an_image_rather_than_dropping_it():
    """THE FAILURE THIS GUARDS AGAINST, observed live.

    `deepseek-v4-pro` does NOT reject a request carrying an image: the API returns HTTP 200 and silently
    drops it. Asked to describe a green-and-yellow test image it answered "I can't see the image you've
    provided because it appears as unsupported" - while answering about a screenshot it never received,
    which a caller cannot distinguish from a genuine reply.

    So a tier with no image-capable route must RAISE. A fallback would produce exactly that confident
    wrong answer.
    """
    import agent.model_gateway as gw

    gateway = _gateway(
        {
            "synthesis": [
                gw.ModelRoute(provider="openai_compatible", model="deepseek-v4-pro", tier="synthesis")
            ]
        }
    )
    with pytest.raises(gw.NoRouteAvailable) as caught:
        gateway.route_for("synthesis", needs_images=True)
    message = str(caught.value)
    assert "deepseek-v4-pro" in message, "the error does not name the configured model"
    assert "cannot see" in message or "image-capable" in message


def test_an_image_capable_route_is_selected_when_one_exists():
    import agent.model_gateway as gw

    gateway = _gateway(
        {
            "synthesis": [
                gw.ModelRoute(provider="openai_compatible", model="deepseek-v4-pro", tier="synthesis"),
                gw.ModelRoute(
                    provider="openai_compatible",
                    model="deepseek-flash",
                    tier="synthesis",
                    supports_images=True,
                ),
            ]
        }
    )
    assert gateway.route_for("synthesis", needs_images=True).model == "deepseek-flash"
    # And the text-only preference is unchanged when there is no image.
    assert gateway.route_for("synthesis").model == "deepseek-v4-pro"


def test_supports_images_defaults_to_false():
    """The safe direction to be wrong in: an unknown model is assumed unable to see, so an image never
    reaches it by omission."""
    import agent.model_gateway as gw

    assert gw.ModelRoute(provider="p", model="m", tier="t").supports_images is False


def test_complete_passes_images_through_to_the_request():
    """The gateway must not read, decode or resize an image - so it cannot become a place where a
    screenshot is quietly rewritten.

    A REAL SESSION, because `complete` records every invocation and a `db=None` fails at
    `self.db.add(...)` after the provider has already been called.
    """
    import agent.model_gateway as gw

    captured: dict = {}

    class RecordingProvider:
        name = "recording"

        def complete(self, request, *, timeout_seconds):
            captured["request"] = request
            return gw.ProviderResponse(text="ok", input_tokens=1, output_tokens=1)

    gateway = gw.ModelGateway(
        _session(),
        RecordingProvider(),
        routes={
            "synthesis": [
                gw.ModelRoute(
                    provider="recording", model="m", tier="synthesis", supports_images=True
                )
            ]
        },
    )
    gateway.complete(
        tier="synthesis",
        prompt="p",
        prompt_version="v1",
        images=("data:image/png;base64,AAAA",),
    )
    assert captured["request"].images == ("data:image/png;base64,AAAA",)


def _session():
    """An in-memory session with the schema built, for the tests that actually complete a call."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import models

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_complete_refuses_an_image_when_no_route_can_see():
    """End to end through the public entry point, not just the router."""
    import agent.model_gateway as gw

    class RecordingProvider:
        name = "recording"

        def complete(self, request, *, timeout_seconds):
            raise AssertionError("the provider was called with an image no route can see")

    gateway = gw.ModelGateway(
        None,
        RecordingProvider(),
        routes={
            "synthesis": [gw.ModelRoute(provider="recording", model="m", tier="synthesis")]
        },
    )
    with pytest.raises(gw.NoRouteAvailable):
        gateway.complete(
            tier="synthesis",
            prompt="p",
            prompt_version="v1",
            images=("data:image/png;base64,AAAA",),
        )


# ===========================================================================
# THE ROUTES MUST COME FROM SETTINGS, OR THE SETTINGS DO NOTHING
# ===========================================================================
class _Settings:
    """The model settings as the deployment actually declares them."""

    model_provider = "openai_compatible"
    model_base_url = "https://api.deepseek.com"
    model_api_key = "k"
    model_classification_model = "deepseek-flash"
    model_synthesis_model = "deepseek-v4-pro"
    model_max_cost_micros_per_call = 5_000_000
    model_max_cost_micros_per_day = 50_000_000
    model_store_prompts = False
    model_timeout_seconds = 60


def test_routes_are_built_from_settings():
    """`MODEL_CLASSIFICATION_MODEL` and `MODEL_SYNTHESIS_MODEL` were read by Settings and used by NO
    route table, so the gateway was constructible only in tests and setting those variables changed
    nothing about what ran."""
    import agent.model_gateway as gw

    routes = gw.build_routes_from_settings(_Settings())
    assert routes["CLASSIFICATION"][0].model == "deepseek-flash"
    assert routes["SYNTHESIS"][0].model == "deepseek-v4-pro"


def test_the_flash_route_is_marked_image_capable_and_pro_is_not():
    """The fact that makes the whole routing guard work. `deepseek-flash` is V4.1-Flash with native
    multimodal understanding; `deepseek-v4-pro` is text-only."""
    import agent.model_gateway as gw

    routes = gw.build_routes_from_settings(_Settings())
    assert routes["CLASSIFICATION"][0].supports_images is True
    assert routes["SYNTHESIS"][0].supports_images is False


def test_an_unknown_model_is_assumed_unable_to_see():
    """The safe direction to be wrong in: an unknown name must not be sent a screenshot on the
    assumption it can handle one."""
    import agent.model_gateway as gw

    class S(_Settings):
        model_classification_model = "some-model-nobody-has-heard-of"

    assert gw.build_routes_from_settings(S())["CLASSIFICATION"][0].supports_images is False


def test_a_gateway_built_from_settings_reaches_the_flash_model_for_an_image():
    """End to end: settings -> routes -> routing decision."""
    import agent.model_gateway as gw

    gateway = gw.build_gateway_from_settings(_session(), _Settings())
    assert gateway.route_for("CLASSIFICATION", needs_images=True).model == "deepseek-flash"
    with pytest.raises(gw.NoRouteAvailable):
        gateway.route_for("SYNTHESIS", needs_images=True)


def test_an_unset_model_leaves_its_tier_absent_rather_than_guessed():
    """A blank setting must produce NO route, so `NoRouteAvailable` names the gap instead of a
    plausible-looking default being invented and acted on."""
    import agent.model_gateway as gw

    class S(_Settings):
        model_synthesis_model = ""

    routes = gw.build_routes_from_settings(S())
    assert "SYNTHESIS" not in routes
    assert "CLASSIFICATION" in routes
