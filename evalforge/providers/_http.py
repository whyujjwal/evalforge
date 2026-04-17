"""Shared HTTP helpers for provider adapters.

Maps ``httpx`` status / network conditions to our error taxonomy so that no
raw third-party exception escapes the provider boundary:

- Connection errors / read timeouts / 5xx / 429  → :class:`ProviderTransientError`
- 4xx other than 429                              → :class:`ProviderPermanentError`
"""

from __future__ import annotations

from typing import Any

import httpx

from evalforge.errors import ProviderPermanentError, ProviderTransientError


def translate_http_error(
    exc: httpx.HTTPError,
    *,
    provider: str,
) -> Exception:
    """Translate an ``httpx`` exception into an evalforge error."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        ctx = {"provider": provider, "status": status}
        if status == 429 or 500 <= status < 600:
            return ProviderTransientError(
                f"{provider} HTTP {status}",
                context=ctx,
            )
        return ProviderPermanentError(
            f"{provider} HTTP {status}",
            context=ctx,
        )
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.RemoteProtocolError,
            httpx.NetworkError,
        ),
    ):
        return ProviderTransientError(
            f"{provider} network error: {type(exc).__name__}: {exc}",
            context={"provider": provider},
        )
    return ProviderPermanentError(
        f"{provider} unknown error: {type(exc).__name__}: {exc}",
        context={"provider": provider},
    )


def require_env(name: str) -> str:
    """Read an env var or raise a :class:`ProviderPermanentError`.

    Providers pass this at construction time so a missing key surfaces
    *before* any request is issued.
    """
    import os

    value = os.environ.get(name)
    if not value:
        raise ProviderPermanentError(
            f"Environment variable {name!r} is not set",
            context={"env": name},
        )
    return value


def redact(value: str | None, *, show: int = 4) -> str:
    """Redact a secret down to a short prefix. Used for debug logging."""
    if not value:
        return ""
    if len(value) <= show:
        return "*" * len(value)
    return value[:show] + "…" + "*" * 6


def json_or_raise(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    try:
        data: Any = response.json()
    except ValueError as e:  # pragma: no cover — unreachable for 2xx JSON APIs
        raise ProviderPermanentError(
            f"Non-JSON response from {response.url}",
            context={"body": response.text[:200]},
        ) from e
    if not isinstance(data, dict):
        raise ProviderPermanentError(
            f"Expected object at top-level from {response.url}",
            context={"type": type(data).__name__},
        )
    return data


__all__ = ["json_or_raise", "redact", "require_env", "translate_http_error"]
