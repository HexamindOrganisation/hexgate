import logging

import pytest

from hexgate.config import env
from hexgate.config.env import resolve_api_key


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEXGATE_API_KEY", raising=False)
    monkeypatch.delenv("HEXGATE_KEY", raising=False)
    # Reset the one-time deprecation-warning gate so each test observes it fresh.
    monkeypatch.setattr(env, "_warned_legacy", False)


def test_explicit_arg_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEXGATE_API_KEY", "from-env")
    assert resolve_api_key("explicit") == "explicit"


def test_reads_primary_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEXGATE_API_KEY", "primary")
    assert resolve_api_key() == "primary"


def test_primary_wins_over_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEXGATE_API_KEY", "primary")
    monkeypatch.setenv("HEXGATE_KEY", "legacy")
    assert resolve_api_key() == "primary"


def test_falls_back_to_legacy_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HEXGATE_KEY", "legacy")
    with caplog.at_level(logging.WARNING, logger="hexgate.config.env"):
        assert resolve_api_key() == "legacy"
    assert any("HEXGATE_KEY is deprecated" in r.message for r in caplog.records)


def test_returns_none_when_unset() -> None:
    assert resolve_api_key() is None
