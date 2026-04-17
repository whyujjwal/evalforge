"""Secret redaction is a correctness property, not a nice-to-have.

This test proves that when an API key is set in the environment, the string
of that key never appears verbatim in any log record emitted through our
structlog pipeline — even if the key is stuffed into a nested dict, a list,
or an exception message.
"""

from __future__ import annotations

import io
import json

import pytest
import structlog
from evalforge._logging import configure, redact_secrets


@pytest.fixture
def captured_logs(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Reconfigure structlog to emit into an in-memory buffer for assertion."""
    buf = io.StringIO()

    def _write(_logger, _method_name, event_dict):
        buf.write(json.dumps(event_dict))
        buf.write("\n")
        return ""

    structlog.configure(
        processors=[
            redact_secrets,
            _write,
        ],
        cache_logger_on_first_use=False,
    )
    yield buf
    # Restore canonical config for subsequent tests.
    configure()


def test_api_key_is_redacted_in_top_level_string(
    captured_logs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecret-abc123")
    log = structlog.get_logger("test")
    log.info("msg", token="Bearer sk-ant-supersecret-abc123 ...")
    out = captured_logs.getvalue()
    assert "sk-ant-supersecret-abc123" not in out
    assert "***REDACTED***" in out


def test_api_key_is_redacted_in_nested_dict(captured_logs, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-xyz987")
    log = structlog.get_logger("test")
    log.info(
        "req",
        headers={"Authorization": "Bearer sk-openai-xyz987"},
        ctx={"keys": ["not-a-secret", "sk-openai-xyz987"]},
    )
    out = captured_logs.getvalue()
    assert "sk-openai-xyz987" not in out


def test_no_env_no_change(captured_logs, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    log = structlog.get_logger("test")
    log.info("just a message", some_string="nothing here")
    out = captured_logs.getvalue()
    assert "nothing here" in out
    assert "REDACTED" not in out
