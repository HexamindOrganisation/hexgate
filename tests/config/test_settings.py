"""Tests for runtime settings."""

from __future__ import annotations

import pytest

from hexgate.config.settings import Settings


def test_from_env_reads_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build settings from environment variables and defaults."""
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("HEXGATE_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("HEXGATE_DEFAULT_SEARCH_ENGINE", raising=False)

    settings = Settings.from_env()

    assert settings.langfuse_host == "https://cloud.langfuse.com"
    assert settings.model == "openai:gpt-5.4"
    assert settings.search_engine == "linkup"


def test_from_env_reads_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env vars override the model/search defaults."""
    monkeypatch.setenv("HEXGATE_DEFAULT_MODEL", "anthropic:claude-opus-4-8")
    monkeypatch.setenv("HEXGATE_DEFAULT_SEARCH_ENGINE", "tavily")

    settings = Settings.from_env()

    assert settings.model == "anthropic:claude-opus-4-8"
    assert settings.search_engine == "tavily"
