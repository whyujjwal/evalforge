"""Error taxonomy.

Three categories the engine treats differently:

- :class:`TransientError` — retried with exponential backoff + jitter, up to
  the per-agent retry cap. Network blips, 5xx, rate limits.
- :class:`PermanentError` — not retried. Task is marked failed, the run
  continues. Bad input, auth failure, schema violation, exhausted retries.
- :class:`FatalError` — aborts the run. Rare: storage unavailable, invariant
  violations, programmer errors.

Provider SDK exceptions (``anthropic.APIError``, ``openai.APIError``, etc.)
are caught at the provider boundary and translated; user code should never
see a raw third-party exception.
"""

from __future__ import annotations

from typing import Any


class EvalforgeError(Exception):
    """Root of the evalforge exception hierarchy.

    Carries optional structured ``context`` that flows into events and logs.
    """

    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context or {})

    def __repr__(self) -> str:
        ctx = f", context={self.context!r}" if self.context else ""
        return f"{type(self).__name__}({self.message!r}{ctx})"


class TransientError(EvalforgeError):
    """A failure that is safe to retry.

    The engine will retry up to the agent's retry cap with exponential backoff
    plus jitter. Use for network glitches, rate limits, and provider 5xx.
    """


class PermanentError(EvalforgeError):
    """A failure that must not be retried.

    The engine marks the task ``failed`` and continues with the rest of the
    run. Use for 4xx responses, schema violations, and exhausted retry caps.
    """


class FatalError(EvalforgeError):
    """A failure that aborts the entire run.

    Use only for invariants that indicate the process cannot make forward
    progress safely: storage unavailable, corruption, contract violations.
    """


class ConfigurationError(PermanentError):
    """Invalid Suite, Pipeline, or Agent configuration detected before execution."""


class ProviderError(EvalforgeError):
    """Base for errors raised by provider adapters.

    Concrete subclasses specialize this into transient or permanent.
    """


class ProviderTransientError(ProviderError, TransientError):
    """Retryable provider failure (rate limit, 5xx, network)."""


class ProviderPermanentError(ProviderError, PermanentError):
    """Non-retryable provider failure (4xx, auth, unknown model)."""


__all__ = [
    "ConfigurationError",
    "EvalforgeError",
    "FatalError",
    "PermanentError",
    "ProviderError",
    "ProviderPermanentError",
    "ProviderTransientError",
    "TransientError",
]
