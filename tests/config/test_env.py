import logging

import pytest

from hexgate.config import env
from hexgate.config.env import resolve_api_key


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEXGATE_API_KEY", raising=False)
    monkeypatch.delenv("HEXGATE_KEY", raising=False)
    # Reset the one-time warning gate so each test observes it fresh.
    monkeypatch.setattr(env, "_warned_legacy", False)


def test_explicit_arg_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEXGATE_API_KEY", "from-env")
    assert resolve_api_key("explicit") == "explicit"


def test_reads_primary_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEXGATE_API_KEY", "primary")
    assert resolve_api_key() == "primary"


def test_legacy_key_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lone HEXGATE_KEY resolves to None — the legacy value is ignored."""
    monkeypatch.setenv("HEXGATE_KEY", "legacy")
    assert resolve_api_key() is None


def test_legacy_key_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HEXGATE_KEY", "legacy")
    with caplog.at_level(logging.WARNING, logger="hexgate.config.env"):
        resolve_api_key()
        resolve_api_key()
    warnings = [r for r in caplog.records if "HEXGATE_KEY is set" in r.message]
    assert len(warnings) == 1


def test_no_legacy_warning_when_primary_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HEXGATE_API_KEY", "primary")
    monkeypatch.setenv("HEXGATE_KEY", "legacy")
    with caplog.at_level(logging.WARNING, logger="hexgate.config.env"):
        assert resolve_api_key() == "primary"
    assert not any("HEXGATE_KEY is set" in r.message for r in caplog.records)


def test_returns_none_when_unset() -> None:
    assert resolve_api_key() is None
