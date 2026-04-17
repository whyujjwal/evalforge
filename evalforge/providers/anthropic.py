"""Anthropic Messages API adapter.

Uses ``httpx`` directly — we do not pull in the vendor SDK. The translation
from evalforge's :class:`CompletionRequest` to Anthropic's wire format is
narrow on purpose; this library is not a general-purpose SDK.

Exceptions from ``httpx`` are translated at this boundary into
:class:`ProviderTransientError` / :class:`ProviderPermanentError`. User code
never sees a raw ``httpx.HTTPError``.
"""

from __future__ import annotations

import httpx

from evalforge.errors import ProviderPermanentError
from evalforge.providers._http import (
    json_or_raise,
    require_env,
    translate_http_error,
)
from evalforge.providers.pricing import estimate_cost_usd
from evalforge.types import CompletionRequest, CompletionResponse, TokenUsage

_DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
_DEFAULT_VERSION = "2023-06-01"


class AnthropicProvider:
    """Provider for Claude models via the Messages API."""

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        version: str = _DEFAULT_VERSION,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._api_key = api_key or require_env("ANTHROPIC_API_KEY")
        self._base_url = base_url.rstrip("/")
        self._version = version
        self._timeout_s = timeout_s
        self._client = client

    async def _client_or_new(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_s)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        client = await self._client_or_new()
        if req.max_tokens is None:
            raise ProviderPermanentError(
                "Anthropic requires max_tokens",
                context={"model": req.model},
            )
        payload: dict[str, object] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "messages": [{"role": "user", "content": req.prompt}],
        }
        if req.system is not None:
            payload["system"] = req.system
        if req.stop:
            payload["stop_sequences"] = list(req.stop)
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._version,
            "content-type": "application/json",
        }
        try:
            response = await client.post(
                f"{self._base_url}/messages", headers=headers, json=payload
            )
        except httpx.HTTPError as e:
            raise translate_http_error(e, provider=self.name) from e
        try:
            data = json_or_raise(response)
        except httpx.HTTPStatusError as e:
            raise translate_http_error(e, provider=self.name) from e

        text = _extract_text(data)
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        cost = estimate_cost_usd(req.model, input_tokens, output_tokens)
        return CompletionResponse(
            text=text,
            tokens=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
            ),
            model=req.model,
            provider=self.name,
            raw={"id": data.get("id"), "stop_reason": data.get("stop_reason")},
        )


def _extract_text(data: dict[str, object]) -> str:
    content = data.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts)


__all__ = ["AnthropicProvider"]
