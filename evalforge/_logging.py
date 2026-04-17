"""Structured logging with secret redaction.

Every evalforge log line is JSON, with ``run_id`` / ``task_id`` / ``node_id``
/ ``event_seq`` in the record whenever they're relevant. A processor redacts
any known API-key environment variable's value before the record is encoded.

This module is intentionally *not* in the public ``__init__.py``; logging is
an implementation detail of the runtime.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_SECRET_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def _current_secrets() -> list[str]:
    return [v for name in _SECRET_ENV_VARS if (v := os.environ.get(name))]


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: redact any known API key value from the record.

    Walks the record dict once and replaces matches with ``"***REDACTED***"``.
    A defense-in-depth measure — provider adapters never log keys in the
    first place, but mistakes happen.
    """
    secrets = _current_secrets()
    if not secrets:
        return event_dict
    for key, value in list(event_dict.items()):
        event_dict[key] = _scrub(value, secrets)
    return event_dict


def _scrub(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        out = value
        for s in secrets:
            if s and s in out:
                out = out.replace(s, "***REDACTED***")
        return out
    if isinstance(value, dict):
        return {k: _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        scrubbed = [_scrub(v, secrets) for v in value]
        return type(value)(scrubbed)
    return value


def configure(*, level: str = "INFO") -> None:
    """Install a JSON-to-stderr structlog pipeline with redaction.

    Idempotent. Safe to call multiple times.
    """
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "evalforge") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


__all__ = ["configure", "get_logger", "redact_secrets"]
