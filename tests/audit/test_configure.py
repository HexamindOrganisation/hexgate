"""audit.configure() — env fallback, explicit args, idempotency."""
from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

import fortify.audit as audit_mod


@pytest.fixture(autouse=True)
def _isolate_audit_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the singleton + clear FORTIFY_* env between tests."""
    audit_mod._sink = None
    monkeypatch.delenv("FORTIFY_KEY", raising=False)
    monkeypatch.delenv("FORTIFY_API_URL", raising=False)
    yield
    audit_mod._sink = None


def test_returns_none_when_no_key_anywhere() -> None:
    assert audit_mod.configure() is None
    assert audit_mod.get_sink() is None


def test_explicit_api_key_uses_default_url() -> None:
    sink = audit_mod.configure("explicit_key")
    assert sink is not None
    assert sink._endpoint == "http://localhost:8000/v1/audit/decisions"


def test_env_api_key_picked_up_when_not_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORTIFY_KEY", "env_key")
    sink = audit_mod.configure()
    assert sink is not None
    assert sink._endpoint == "http://localhost:8000/v1/audit/decisions"


def test_explicit_api_key_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORTIFY_KEY", "env_key")
    sink = audit_mod.configure("explicit_key")
    assert sink._client.headers["Authorization"] == "Bearer explicit_key"


def test_env_base_url_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORTIFY_API_URL", "https://prod.example.com/")
    sink = audit_mod.configure("k")
    assert sink._endpoint == "https://prod.example.com/v1/audit/decisions"


def test_explicit_base_url_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORTIFY_API_URL", "https://env.example.com")
    sink = audit_mod.configure("k", "https://explicit.example.com")
    assert sink._endpoint == "https://explicit.example.com/v1/audit/decisions"


def test_idempotent_returns_first_sink() -> None:
    first = audit_mod.configure("k1")
    second = audit_mod.configure("k2")  # later call with different args
    assert first is second
    # And the first key is what was kept.
    assert first._client.headers["Authorization"] == "Bearer k1"
