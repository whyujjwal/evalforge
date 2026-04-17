"""Provider adapter smoke tests.

No real network calls — we stub ``httpx.AsyncClient`` via the ``client``
constructor kwarg and verify:

- Request payload shape matches each vendor's schema.
- Response parsing extracts text and token counts correctly.
- HTTP errors are translated to our taxonomy at the boundary.
- Environment-based auth raises at construction when unset.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from evalforge.errors import (
    ProviderPermanentError,
    ProviderTransientError,
)
from evalforge.providers.anthropic import AnthropicProvider
from evalforge.providers.openai import OpenAIProvider
from evalforge.types import CompletionRequest


def _fake_client(*, status: int, json_body: dict[str, Any]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=json_body, request=request)

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


async def test_anthropic_request_shape_and_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["body"] = request.read()
        import json

        captured["json"] = json.loads(captured["body"])
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "content": [{"type": "text", "text": "hello world"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(api_key="sk-test", client=client)
    resp = await provider.complete(
        CompletionRequest(
            model="claude-sonnet-4-5",
            prompt="hi",
            temperature=0.2,
            max_tokens=32,
            system="be nice",
        )
    )
    assert resp.text == "hello world"
    assert resp.tokens.input_tokens == 10
    assert resp.tokens.output_tokens == 3
    assert resp.tokens.cost_usd > 0  # priced
    assert captured["json"]["model"] == "claude-sonnet-4-5"
    assert captured["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["json"]["system"] == "be nice"
    assert captured["headers"]["x-api-key"] == "sk-test"
    await provider.close()


async def test_anthropic_requires_max_tokens() -> None:
    provider = AnthropicProvider(api_key="sk-test")
    with pytest.raises(ProviderPermanentError):
        await provider.complete(
            CompletionRequest(model="claude-sonnet-4-5", prompt="x", max_tokens=None)
        )
    await provider.close()


async def test_anthropic_transient_on_429() -> None:
    client = _fake_client(status=429, json_body={"error": "rate limited"})
    provider = AnthropicProvider(api_key="sk-test", client=client)
    with pytest.raises(ProviderTransientError):
        await provider.complete(
            CompletionRequest(model="claude-sonnet-4-5", prompt="x", max_tokens=4)
        )
    await provider.close()


async def test_anthropic_permanent_on_400() -> None:
    client = _fake_client(status=400, json_body={"error": "bad request"})
    provider = AnthropicProvider(api_key="sk-test", client=client)
    with pytest.raises(ProviderPermanentError):
        await provider.complete(
            CompletionRequest(model="claude-sonnet-4-5", prompt="x", max_tokens=4)
        )
    await provider.close()


async def test_anthropic_transient_on_5xx() -> None:
    client = _fake_client(status=503, json_body={"error": "unavailable"})
    provider = AnthropicProvider(api_key="sk-test", client=client)
    with pytest.raises(ProviderTransientError):
        await provider.complete(
            CompletionRequest(model="claude-sonnet-4-5", prompt="x", max_tokens=4)
        )
    await provider.close()


async def test_openai_request_shape_and_parse() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["json"] = json.loads(request.read())
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "id": "resp-1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "42"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(api_key="sk-openai", client=client)
    resp = await provider.complete(
        CompletionRequest(model="gpt-4o-mini", prompt="what is the answer?")
    )
    assert resp.text == "42"
    assert resp.tokens.input_tokens == 5
    assert resp.tokens.output_tokens == 1
    assert captured["json"]["model"] == "gpt-4o-mini"
    assert captured["headers"]["authorization"] == "Bearer sk-openai"
    await provider.close()


async def test_openai_transient_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(api_key="sk-openai", client=client)
    with pytest.raises(ProviderTransientError):
        await provider.complete(CompletionRequest(model="gpt-4o-mini", prompt="x"))
    await provider.close()


async def test_provider_refuses_to_build_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderPermanentError):
        AnthropicProvider()
    with pytest.raises(ProviderPermanentError):
        OpenAIProvider()
