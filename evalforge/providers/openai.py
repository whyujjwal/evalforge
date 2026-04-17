"""OpenAI Chat Completions adapter.

Uses ``httpx`` directly — no vendor SDK dependency. Exceptions are translated
at the boundary into :class:`ProviderTransientError` /
:class:`ProviderPermanentError`.
"""

from __future__ import annotations

import httpx

from evalforge.providers._http import (
    json_or_raise,
    require_env,
    translate_http_error,
)
from evalforge.providers.pricing import estimate_cost_usd
from evalforge.types import CompletionRequest, CompletionResponse, TokenUsage

_DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider:
    """Provider for GPT models via the Chat Completions API."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 60.0,
        organization: str | None = None,
    ) -> None:
        self._api_key = api_key or require_env("OPENAI_API_KEY")
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._organization = organization
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
        messages: list[dict[str, str]] = []
        if req.system is not None:
            messages.append({"role": "system", "content": req.system})
        messages.append({"role": "user", "content": req.prompt})
        payload: dict[str, object] = {
            "model": req.model,
            "messages": messages,
            "temperature": req.temperature,
        }
        if req.max_tokens is not None:
            payload["max_tokens"] = req.max_tokens
        if req.stop:
            payload["stop"] = list(req.stop)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        if self._organization:
            headers["OpenAI-Organization"] = self._organization
        try:
            response = await client.post(
                f"{self._base_url}/chat/completions", headers=headers, json=payload
            )
        except httpx.HTTPError as e:
            raise translate_http_error(e, provider=self.name) from e
        try:
            data = json_or_raise(response)
        except httpx.HTTPStatusError as e:
            raise translate_http_error(e, provider=self.name) from e

        text = _extract_text(data)
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
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
            raw={"id": data.get("id"), "finish_reason": _first_finish_reason(data)},
        )


def _extract_text(data: dict[str, object]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    msg = first.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
    return ""


def _first_finish_reason(data: dict[str, object]) -> str | None:
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        return reason if isinstance(reason, str) else None
    return None


__all__ = ["OpenAIProvider"]
