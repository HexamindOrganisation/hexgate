"""emit_llm_messages() — the single entry point every adapter's message hook
calls into. Covers identity resolution from the HexgateContext contextvar,
the ``HEXGATE_LOG_MESSAGES`` opt-out, the no-op path when no sender is
configured, and the never-raises contract; the sender itself is faked so no
real registry/network is touched (mirrors tests/tracing/test_usage_emit.py).

The wiring tests at the bottom use the real registry to prove this module
shares it — and the ``HEXGATE_LOCAL_MODE`` gate — with decisions and usage
rather than duplicating it."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import hexgate.audit as audit_mod
import hexgate.tracing.usage as usage_mod
from hexgate.runtime import HexgateContext
from hexgate.runtime.run_facts import run_scope
from hexgate.tracing import _senders
from hexgate.tracing import messages as messages_mod
from hexgate.tracing import semconv
from hexgate.tracing.messages import LOG_MESSAGES_ENV, emit_llm_messages

_INPUT = [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}]
_OUTPUT = [{"role": "assistant", "parts": [{"type": "text", "content": "hello"}]}]


class _FakeSender:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def _isolate_sender_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the shared sender registry + clear HEXGATE_* env between tests."""
    _senders._senders.clear()
    _senders._logged_local_mode_suppressed = False
    monkeypatch.delenv("HEXGATE_API_KEY", raising=False)
    monkeypatch.delenv("HEXGATE_API_URL", raising=False)
    monkeypatch.delenv(_senders._LOCAL_MODE_ENV, raising=False)
    monkeypatch.delenv(LOG_MESSAGES_ENV, raising=False)
    yield
    _senders._senders.clear()
    _senders._logged_local_mode_suppressed = False


def _install_fake(monkeypatch: pytest.MonkeyPatch) -> _FakeSender:
    fake_sender = _FakeSender()
    monkeypatch.setattr(
        messages_mod, "configure_messages_sender", lambda api_key=None: fake_sender
    )
    return fake_sender


def _emit(**overrides: Any) -> None:
    kwargs: dict[str, Any] = dict(turn_key="run-1", message_seq=0, api_key="k")
    kwargs.update(overrides)
    emit_llm_messages("my-agent", "gpt-4o", _INPUT, _OUTPUT, **kwargs)


def test_emit_llm_messages_is_a_noop_when_no_sender_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        messages_mod, "configure_messages_sender", lambda api_key=None: None
    )

    _emit()  # must not raise


def test_emit_llm_messages_sends_event_with_given_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sender = _install_fake(monkeypatch)
    system = [{"type": "text", "content": "Be brief."}]

    _emit(message_seq=2, system_instructions=system, resynced=True)

    [event] = fake_sender.events
    assert event.SCOPE == semconv.SCOPE_MESSAGES
    assert event.agent_name == "my-agent"
    assert event.model == "gpt-4o"
    assert event.input_messages == _INPUT
    assert event.output_messages == _OUTPUT
    assert event.turn_key == "run-1"
    assert event.message_seq == 2
    assert event.system_instructions == system
    assert event.resynced is True
    assert event.user_id == ""
    assert event.session_id == ""


async def test_emit_llm_messages_resolves_identity_from_active_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sender = _install_fake(monkeypatch)

    async with HexgateContext(user_id="alice", session_id="sess-1", user_roles=["dev"]):
        _emit()

    [event] = fake_sender.events
    assert event.user_id == "alice"
    assert event.session_id == "sess-1"


def test_when_sender_emit_fails_then_emit_llm_messages_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter hooks this runs from either re-raise on an unhandled
    exception or don't guard the call at all — a failure here must not fail
    the agent run whose messages it is logging."""

    class _RaisingSender:
        def emit(self, event: Any) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(
        messages_mod, "configure_messages_sender", lambda api_key=None: _RaisingSender()
    )

    _emit()  # must not raise


def test_emit_llm_messages_passes_api_key_to_configure_messages_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str | None] = []

    def fake_configure(api_key: str | None = None) -> _FakeSender:
        captured.append(api_key)
        return _FakeSender()

    monkeypatch.setattr(messages_mod, "configure_messages_sender", fake_configure)

    _emit(api_key="explicit-key")

    assert captured == ["explicit-key"]


def test_emit_stamps_the_enclosing_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """llm_message rows join to the policy_decision and llm_invocation rows of
    the same run, so the run_id has to reach the event."""
    fake_sender = _install_fake(monkeypatch)

    with run_scope("agent") as facts:
        _emit()

    [event] = fake_sender.events
    assert event.run_id == facts.id
    assert event.span_attributes()[semconv.RUN_ID] == facts.id


def test_emit_outside_a_run_scope_sends_no_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sender = _install_fake(monkeypatch)

    _emit()

    [event] = fake_sender.events
    assert event.run_id == ""
    assert semconv.RUN_ID not in event.span_attributes()


# ---------------------------------------------------------------------------
# HEXGATE_LOG_MESSAGES — on by default, opt out with 0
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " 0 "])
def test_when_log_messages_is_off_then_nothing_is_emitted(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    fake_sender = _install_fake(monkeypatch)
    monkeypatch.setenv(LOG_MESSAGES_ENV, value)

    _emit()

    assert fake_sender.events == []


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "", "anything"])
def test_when_log_messages_is_not_a_falsy_value_then_capture_stays_on(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Only an explicit "off" spelling opts out; an empty or unrecognised
    value keeps the default so a typo never silently disables the log."""
    fake_sender = _install_fake(monkeypatch)
    monkeypatch.setenv(LOG_MESSAGES_ENV, value)

    _emit()

    assert len(fake_sender.events) == 1


def test_when_opted_out_then_no_sender_is_created_for_this_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opt-out is checked before the registry, so a process that only
    ever emits messages and has them off never starts an export worker."""
    calls: list[str | None] = []

    def fake_configure(api_key: str | None = None) -> _FakeSender:
        calls.append(api_key)
        return _FakeSender()

    monkeypatch.setattr(messages_mod, "configure_messages_sender", fake_configure)
    monkeypatch.setenv(LOG_MESSAGES_ENV, "0")

    _emit()

    assert calls == []


def test_opt_out_is_read_at_emit_time_not_at_import_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sender = _install_fake(monkeypatch)

    _emit()
    monkeypatch.setenv(LOG_MESSAGES_ENV, "0")
    _emit()
    monkeypatch.delenv(LOG_MESSAGES_ENV)
    _emit()

    assert len(fake_sender.events) == 2


def test_opt_out_leaves_the_other_streams_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HEXGATE_LOG_MESSAGES=0`` switches off message content only; token
    usage keeps flowing through the same registry."""
    fake_sender = _FakeSender()
    monkeypatch.setattr(
        messages_mod, "configure_messages_sender", lambda api_key=None: fake_sender
    )
    monkeypatch.setattr(
        usage_mod, "configure_usage_sender", lambda api_key=None: fake_sender
    )
    monkeypatch.setenv(LOG_MESSAGES_ENV, "0")

    _emit()
    usage_mod.emit_llm_usage("my-agent", "gpt-4o", 10, 20, api_key="k")

    [event] = fake_sender.events
    assert event.SCOPE == semconv.SCOPE_USAGE


# ---------------------------------------------------------------------------
# configure_messages_sender() — thin wiring onto the shared registry
# ---------------------------------------------------------------------------


def test_configure_messages_sender_returns_none_when_no_key_anywhere() -> None:
    assert messages_mod.configure_messages_sender() is None
    assert messages_mod.get_messages_sender() is None


def test_same_key_shares_the_sender_with_audit_and_usage() -> None:
    """One HEXGATE_API_KEY, one sender: decisions, usage and messages all
    export through it; the span's instrumentation scope keeps them apart."""
    decisions_sender = audit_mod.configure("k1")
    usage_sender = usage_mod.configure_usage_sender("k1")
    messages_sender = messages_mod.configure_messages_sender("k1")
    assert decisions_sender is usage_sender is messages_sender


def test_local_mode_suppresses_the_messages_sender_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_senders._LOCAL_MODE_ENV, "1")
    assert messages_mod.configure_messages_sender("k1") is None


def test_emit_llm_messages_is_a_noop_in_local_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real registry, not a fake: local mode is enforced by the shared
    ``get_or_create_sender`` and this module must not route around it."""
    monkeypatch.setenv(_senders._LOCAL_MODE_ENV, "1")

    _emit()

    assert _senders._senders == {}
