"""audit.configure() — env fallback, explicit args, idempotency."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

import fortify.audit as audit_mod


@pytest.fixture(autouse=True)
def _isolate_audit_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the sender registry + clear FORTIFY_* env between tests."""
    audit_mod._senders.clear()
    audit_mod._logged_local_mode_suppressed = False
    monkeypatch.delenv("FORTIFY_KEY", raising=False)
    monkeypatch.delenv("FORTIFY_API_URL", raising=False)
    monkeypatch.delenv(audit_mod._LOCAL_MODE_ENV, raising=False)
    yield
    audit_mod._senders.clear()
    audit_mod._logged_local_mode_suppressed = False


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


# ---------------------------------------------------------------------------
# FORTIFY_LOCAL_MODE gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on"])
def test_local_mode_env_suppresses_configure(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    """Any truthy value of FORTIFY_LOCAL_MODE makes configure() return None
    even when an api_key is in env. The gate is the whole point of local
    mode: a key in .env (left over from a platform session) must not cause
    cloud writes the next time the dev runs `fortify chat`."""
    monkeypatch.setenv("FORTIFY_KEY", "real_key")
    monkeypatch.setenv(audit_mod._LOCAL_MODE_ENV, truthy)
    assert audit_mod.configure() is None
    assert audit_mod.get_sender("real_key") is None


@pytest.mark.parametrize("falsy", ["0", "false", "no", "off", ""])
def test_local_mode_falsy_does_not_suppress(
    monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    """Explicit falsy values behave as if unset — symmetry with the
    truthy parametrize prevents a future refactor from accidentally
    making `FORTIFY_LOCAL_MODE=0` count as 'on'."""
    monkeypatch.setenv("FORTIFY_KEY", "real_key")
    monkeypatch.setenv(audit_mod._LOCAL_MODE_ENV, falsy)
    sender = audit_mod.configure()
    assert sender is not None


def test_local_mode_explicit_key_arg_still_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate fires even when a caller passes an explicit api_key —
    adapter wrappers that do `audit.configure(api_key=...)` post-bootstrap
    must respect local mode too."""
    monkeypatch.setenv(audit_mod._LOCAL_MODE_ENV, "1")
    assert audit_mod.configure("explicit_key") is None


def test_local_mode_logs_suppression_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A single INFO line at the first suppression; further configure()
    calls stay silent so a busy startup doesn't repeat itself."""
    monkeypatch.setenv("FORTIFY_KEY", "real_key")
    monkeypatch.setenv(audit_mod._LOCAL_MODE_ENV, "1")
    with caplog.at_level(logging.INFO, logger="fortify.audit"):
        audit_mod.configure()
        audit_mod.configure()
        audit_mod.configure()
    suppressed = [
        r for r in caplog.records if "audit suppressed" in r.message
    ]
    assert len(suppressed) == 1


def test_local_mode_silent_when_no_key_present(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No key + local mode is the OSS "I never set a key" case — no log
    line, because there's no surprise to disambiguate."""
    monkeypatch.setenv(audit_mod._LOCAL_MODE_ENV, "1")
    with caplog.at_level(logging.INFO, logger="fortify.audit"):
        audit_mod.configure()
    suppressed = [
        r for r in caplog.records if "audit suppressed" in r.message
    ]
    assert suppressed == []
