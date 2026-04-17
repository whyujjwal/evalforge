"""Shared pytest fixtures and safety guards.

Enforces that no real LLM provider is instantiated in the test suite.
The engine tests use `providers.mock`; any import path that ends up hitting
`providers.anthropic.AnthropicProvider` or `providers.openai.OpenAIProvider`
in strict mode will raise and fail the test.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _no_real_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub env so no real provider client can initialize with credentials."""
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EVALFORGE_TEST_STRICT", os.environ.get("EVALFORGE_TEST_STRICT", "1"))
