"""PolicyEnforcer emission paths into the audit sink."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

import fortify.audit as audit_mod
from fortify.audit import AuditEvent, AuditSink
from fortify.runtime.context import User
from fortify.security.decision import DecisionOutcome, Verdict
from fortify.security.enforcer import PolicyEnforcer


class _StubEngine:
    def evaluate(self, *, role: str | None, tool: str, args: Mapping[str, Any]) -> Verdict:
        return Verdict(outcome=DecisionOutcome.DENY, reason="stub")


class _CapturingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def _reset_audit_sink() -> None:
    audit_mod._sink = None
    yield
    audit_mod._sink = None


def test_no_sink_means_no_emit() -> None:
    enforcer = PolicyEnforcer(_StubEngine(), agent_name="r")
    decision = enforcer.decide("read_file", {"path": "/x"})
    assert decision.outcome is DecisionOutcome.DENY
    assert audit_mod.get_sink() is None


def test_sink_no_user_emits_with_empty_envelope() -> None:
    sink = _CapturingSink()
    audit_mod._sink = sink
    PolicyEnforcer(_StubEngine(), agent_name="r").decide("read_file", {})
    assert len(sink.events) == 1
    assert sink.events[0].user_id == ""
    assert sink.events[0].session_id == ""


async def test_sink_with_user_populates_envelope_from_user() -> None:
    sink = _CapturingSink()
    audit_mod._sink = sink
    enforcer = PolicyEnforcer(_StubEngine(), agent_name="r")
    async with User(user_id="alice", role="analyst", session_id="sess_42"):
        decision = enforcer.decide("read_file", {})
    ev = sink.events[0]
    assert decision.role == "analyst"   # role propagates from User to Decision
    assert ev.user_id == "alice"
    assert ev.session_id == "sess_42"
    assert ev.decision is decision      # same Decision instance wrapped


async def test_user_session_id_none_normalizes_to_empty_string() -> None:
    sink = _CapturingSink()
    audit_mod._sink = sink
    enforcer = PolicyEnforcer(_StubEngine(), agent_name="r")
    async with User(user_id="bob", role="reader"):  # session_id defaults to None
        enforcer.decide("read_file", {})
    ev = sink.events[0]
    assert ev.user_id == "bob"
    assert ev.session_id == ""


def test_capturing_sink_satisfies_audit_sink_protocol() -> None:
    """Structural typing: anything with emit() is an AuditSink."""
    assert isinstance(_CapturingSink(), AuditSink)
