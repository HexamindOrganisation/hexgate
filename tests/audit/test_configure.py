"""audit.configure() — env fallback, explicit args, idempotency."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import fortify.audit as audit_mod


@pytest.fixture(autouse=True)
def _isolate_audit_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the sender registry + clear FORTIFY_* env between tests."""
    audit_mod._senders.clear()
    monkeypatch.delenv("FORTIFY_KEY", raising=False)
    monkeypatch.delenv("FORTIFY_API_URL", raising=False)
    yield
    audit_mod._senders.clear()


def test_returns_none_when_no_key_anywhere() -> None:
    assert audit_mod.configure() is None
    assert audit_mod.get_sender() is None


def test_explicit_api_key_uses_default_url() -> None:
    sender = audit_mod.configure("explicit_key")
    assert sender is not None
    assert sender._endpoint == "http://localhost:8000/v1/audit/decisions"


def test_env_api_key_picked_up_when_not_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORTIFY_KEY", "env_key")
    sender = audit_mod.configure()
    assert sender is not None
    assert sender._endpoint == "http://localhost:8000/v1/audit/decisions"


def test_explicit_api_key_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORTIFY_KEY", "env_key")
    sender = audit_mod.configure("explicit_key")
    assert sender._client.headers["Authorization"] == "Bearer explicit_key"


def test_env_base_url_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORTIFY_API_URL", "https://prod.example.com/")
    sender = audit_mod.configure("k")
    assert sender._endpoint == "https://prod.example.com/v1/audit/decisions"


def test_explicit_base_url_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORTIFY_API_URL", "https://env.example.com")
    sender = audit_mod.configure("k", "https://explicit.example.com")
    assert sender._endpoint == "https://explicit.example.com/v1/audit/decisions"


def test_idempotent_per_key_returns_same_sender() -> None:
    first = audit_mod.configure("k1")
    second = audit_mod.configure("k1")  # same key → reuse
    assert first is second


def test_distinct_keys_get_distinct_senders() -> None:
    sender_a = audit_mod.configure("k1")
    sender_b = audit_mod.configure("k2")
    assert sender_a is not sender_b
    assert sender_a._client.headers["Authorization"] == "Bearer k1"
    assert sender_b._client.headers["Authorization"] == "Bearer k2"


def test_get_sender_scoped_by_key() -> None:
    sender = audit_mod.configure("k1")
    assert audit_mod.get_sender("k1") is sender
    assert audit_mod.get_sender("k2") is None
