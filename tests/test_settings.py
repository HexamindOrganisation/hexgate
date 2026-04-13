"""Tests for runtime settings."""

from __future__ import annotations

import pytest

from asianf.config.settings import Settings


def test_from_env_reads_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build settings from environment variables and defaults."""
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("LINKUP_API_KEY", "linkup-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("ASIANF_MODEL", raising=False)
    monkeypatch.delenv("ASIANF_SEARCH_ENGINE", raising=False)

    settings = Settings.from_env()

    assert settings.openai_api_key == "openai-key"
    assert settings.linkup_api_key == "linkup-key"
    assert settings.tavily_api_key == "tavily-key"
    assert settings.langfuse_host == "https://cloud.langfuse.com"
    assert settings.model == "openai:gpt-5.4"
    assert settings.search_engine == "linkup"


def test_validate_required_keys_raises_for_missing_keys() -> None:
    """Raise a helpful error when required keys are missing."""
    settings = Settings(
        openai_api_key=None,
        linkup_api_key=None,
        tavily_api_key=None,
        langfuse_public_key=None,
        langfuse_secret_key=None,
        langfuse_host="https://cloud.langfuse.com",
        model="openai:gpt-5.4",
        search_engine="linkup",
    )

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY, LINKUP_API_KEY, TAVILY_API_KEY"):
        settings.validate_required_keys()


def test_validate_required_keys_accepts_present_keys() -> None:
    """Allow valid settings through without raising."""
    settings = Settings(
        openai_api_key="openai-key",
        linkup_api_key="linkup-key",
        tavily_api_key="tavily-key",
        langfuse_public_key=None,
        langfuse_secret_key=None,
        langfuse_host="https://cloud.langfuse.com",
        model="openai:gpt-5.4",
        search_engine="linkup",
    )

    settings.validate_required_keys()
