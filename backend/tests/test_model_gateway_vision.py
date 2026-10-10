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
