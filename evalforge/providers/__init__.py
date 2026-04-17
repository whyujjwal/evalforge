"""Provider registry and protocol.

A :class:`Provider` is a minimal adapter around a specific LLM vendor. The
engine never speaks vendor-native protocols directly — it speaks to a
:class:`~evalforge.types.CompletionRequest` / :class:`~evalforge.types.CompletionResponse`
pair, and the provider handles translation at the boundary.

Provider SDK exceptions must be caught and translated to
:class:`~evalforge.errors.ProviderTransientError` or
:class:`~evalforge.errors.ProviderPermanentError` at this seam. User code must
never see a raw ``anthropic.APIError`` or ``openai.APIError``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from evalforge.errors import ConfigurationError
from evalforge.types import CompletionRequest, CompletionResponse


@runtime_checkable
class Provider(Protocol):
    """Minimal LLM provider interface.

    Implementations must be async and stateless except for client caches.
    """

    name: str

    async def complete(self, req: CompletionRequest) -> CompletionResponse: ...


_REGISTRY: dict[str, Provider] = {}


def register_provider(provider: Provider) -> None:
    """Register a provider instance by its ``name``.

    Registering the same name twice is allowed and replaces the prior entry;
    this is deliberate so tests can swap in a mock.
    """
    _REGISTRY[provider.name] = provider


def get_provider(name: str) -> Provider:
    try:
        return _REGISTRY[name]
    except KeyError as e:
        raise ConfigurationError(
            f"No provider registered under name={name!r}",
            context={"registered": sorted(_REGISTRY.keys())},
        ) from e


def clear_registry() -> None:
    """Test helper. Not part of the public API."""
    _REGISTRY.clear()


def registered_names() -> list[str]:
    return sorted(_REGISTRY.keys())


__all__ = [
    "Provider",
    "clear_registry",
    "get_provider",
    "register_provider",
    "registered_names",
]
