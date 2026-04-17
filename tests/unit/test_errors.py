"""Error taxonomy wiring."""

from __future__ import annotations

from evalforge.errors import (
    ConfigurationError,
    EvalforgeError,
    FatalError,
    PermanentError,
    ProviderPermanentError,
    ProviderTransientError,
    TransientError,
)


def test_hierarchy() -> None:
    assert issubclass(TransientError, EvalforgeError)
    assert issubclass(PermanentError, EvalforgeError)
    assert issubclass(FatalError, EvalforgeError)
    assert issubclass(ConfigurationError, PermanentError)
    assert issubclass(ProviderTransientError, TransientError)
    assert issubclass(ProviderPermanentError, PermanentError)


def test_context_flows_through() -> None:
    err = TransientError("boom", context={"attempt": 2, "url": "https://x"})
    assert err.context == {"attempt": 2, "url": "https://x"}
    assert "boom" in repr(err)


def test_context_is_copied_not_aliased() -> None:
    ctx = {"a": 1}
    err = PermanentError("x", context=ctx)
    ctx["a"] = 999
    assert err.context == {"a": 1}


def test_message_is_preserved() -> None:
    err = FatalError("unrecoverable")
    assert err.message == "unrecoverable"
    assert str(err) == "unrecoverable"
