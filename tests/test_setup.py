"""Tests for bootstrap helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from coolagents import setup


def test_bootstrap_loads_requested_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load environment values from the requested repo-relative env file."""
    seen: dict[str, Path] = {}

    def fake_load_dotenv(path: Path, override: bool) -> None:
        """Capture the env file path and populate environment values."""
        seen["path"] = path
        assert override is True
        monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
        monkeypatch.setenv("LINKUP_API_KEY", "linkup-key")
        monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public-key")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret-key")

    monkeypatch.setattr(setup, "load_dotenv", fake_load_dotenv)

    settings = setup.bootstrap("test.env")

    assert seen["path"] == Path(setup.__file__).parent.parent / "test.env"
    assert settings.openai_api_key == "openai-key"
    assert settings.linkup_api_key == "linkup-key"
    assert settings.tavily_api_key == "tavily-key"
    assert settings.langfuse_public_key == "public-key"
    assert settings.langfuse_secret_key == "secret-key"
