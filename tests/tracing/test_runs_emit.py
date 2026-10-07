"""emit_run_start(): the sender is faked through the module's
``get_or_create_sender`` seam, so no registry or network is touched."""

from __future__ import annotations

from typing import Any

import pytest

from hexgate.tracing import runs as runs_mod
from hexgate.tracing.runs import RunStartEvent, emit_run_start


class _FakeSender:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


class _RaisingSender:
    def emit(self, event: Any) -> None:
        raise RuntimeError("queue exploded")


def test_is_a_noop_when_no_sender_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runs_mod, "get_or_create_sender", lambda api_key=None: None)

    emit_run_start("agent", "run-1", api_key="k")  # must not raise


def test_swallows_a_sender_that_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runs_mod, "get_or_create_sender", lambda api_key=None: _RaisingSender()
    )

    emit_run_start("agent", "run-1", api_key="k")  # must not raise


def test_swallows_a_sender_lookup_that_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(api_key: str | None = None) -> None:
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(runs_mod, "get_or_create_sender", _boom)

    emit_run_start("agent", "run-1", api_key="k")  # must not raise


def test_sends_one_event_with_the_given_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sender = _FakeSender()
    monkeypatch.setattr(
        runs_mod, "get_or_create_sender", lambda api_key=None: fake_sender
    )

    emit_run_start("agent", "run-1", session_id="sess", user_id="alice")

    [event] = fake_sender.events
    assert isinstance(event, RunStartEvent)
    assert (event.agent_name, event.run_id) == ("agent", "run-1")
    assert (event.session_id, event.user_id) == ("sess", "alice")


def test_forwards_the_api_key_to_the_sender_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str | None] = []

    def _lookup(api_key: str | None = None) -> _FakeSender:
        seen.append(api_key)
        return _FakeSender()

    monkeypatch.setattr(runs_mod, "get_or_create_sender", _lookup)

    emit_run_start("agent", "run-1", api_key="agent-key")

    assert seen == ["agent-key"]
