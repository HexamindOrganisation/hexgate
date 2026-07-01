"""Tests for runtime settings."""

from __future__ import annotations

import pytest

from hexgate.config.settings import Settings


def test_from_env_reads_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build settings from environment variables and defaults."""
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("LINKUP_API_KEY", "linkup-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("HEXGATE_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("HEXGATE_DEFAULT_SEARCH_ENGINE", raising=False)

    settings = Settings.from_env()

    assert settings.openai_api_key == "openai-key"
    assert settings.linkup_api_key == "linkup-key"
    assert settings.tavily_api_key == "tavily-key"
    assert settings.langfuse_host == "https://cloud.langfuse.com"
    assert settings.model == "openai:gpt-5.4"
    assert settings.search_engine == "linkup"


def test_from_env_leaves_unset_keys_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing provider keys resolve to None — bootstrap never hard-fails on
    them; the tool or model provider raises at use-time instead."""
    for key in ("OPENAI_API_KEY", "LINKUP_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    settings = Settings.from_env()

    assert settings.openai_api_key is None
    assert settings.linkup_api_key is None
    assert settings.tavily_api_key is None
