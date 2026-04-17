"""Mock provider for deterministic tests and examples.

Accepts a scripted list of responses, a callable, or a default echo. Real
provider adapters must not import this module; this is for tests and the
built-in example suite only.

Also supports injecting transient/permanent failures — this is how engine
tests exercise retry behavior without touching a real network.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Union

import anyio

from evalforge.errors import ProviderPermanentError, ProviderTransientError
from evalforge.providers import Provider, register_provider
from evalforge.types import CompletionRequest, CompletionResponse, TokenUsage

MockScript = Union[
    str,
    CompletionResponse,
    type[Exception],
    Exception,
    Callable[[CompletionRequest], "str | CompletionResponse"],
]


@dataclass
class MockProvider:
    """A deterministic, seedable provider.

    ``script`` is consumed in order, one entry per ``complete()`` call. When
    the script is exhausted we loop back to the start — this makes it easy to
    hand the provider a single response and run N tasks against it.

    Each entry may be:

    - ``str`` — returned as ``CompletionResponse.text``
    - :class:`CompletionResponse` — returned verbatim (after provider-name
      rewrite)
    - an ``Exception`` instance or class — raised
    - a callable ``(CompletionRequest) -> str | CompletionResponse`` — invoked
      (useful for property-style mocks)
    """

    name: str = "mock"
    script: list[MockScript] = field(default_factory=list)
    latency_s: float = 0.0
    tokens_per_call: TokenUsage = field(
        default_factory=lambda: TokenUsage(input_tokens=5, output_tokens=5)
    )
    _cursor: int = 0
    calls: list[CompletionRequest] = field(default_factory=list)

    def reset(self) -> None:
        self._cursor = 0
        self.calls.clear()

    def _next(self) -> MockScript:
        if not self.script:
            return ""
        entry = self.script[self._cursor % len(self.script)]
        self._cursor += 1
        return entry

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.calls.append(req)
        if self.latency_s:
            await anyio.sleep(self.latency_s)
        entry: Any = self._next()

        if isinstance(entry, type) and issubclass(entry, Exception):
            raise entry("mock scripted exception")
        if isinstance(entry, Exception):
            raise entry
        if callable(entry) and not isinstance(entry, CompletionResponse):
            entry = entry(req)

        if isinstance(entry, CompletionResponse):
            return entry.model_copy(update={"provider": self.name})
        text = str(entry) if entry != "" else f"[mock echo] {req.prompt}"
        return CompletionResponse(
            text=text,
            tokens=self.tokens_per_call,
            model=req.model,
            provider=self.name,
            raw={"mock": True},
        )


def install_mock_provider(
    *,
    name: str = "mock",
    script: list[MockScript] | None = None,
    latency_s: float = 0.0,
) -> MockProvider:
    """Register a fresh :class:`MockProvider` under ``name`` and return it."""
    mp = MockProvider(name=name, script=list(script or []), latency_s=latency_s)
    register_provider(mp)
    return mp


__all__ = [
    "MockProvider",
    "Provider",
    "ProviderPermanentError",
    "ProviderTransientError",
    "install_mock_provider",
]
