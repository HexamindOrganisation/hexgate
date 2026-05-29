"""AuditEvent.as_payload() field mapping for the platform's audit endpoint."""
from __future__ import annotations

from fortify.audit import AuditEvent
from fortify.security.decision import Decision, DecisionOutcome


def _decision(**overrides) -> Decision:
    base = dict(
        outcome=DecisionOutcome.DENY, agent_name="researcher", tool_name="read_file"
    )
    return Decision(**{**base, **overrides})


def test_as_payload_full_payload() -> None:
    d = _decision(
        role="analyst",
        reason="denied for path",
        error_type="policy_denied",
        hint={"glob": "/x/**"},
        violations=("v1", "v2"),
        arguments={"path": "/etc/passwd"},
    )
    ev = AuditEvent(decision=d, user_id="alice", session_id="sess_1")
    wire = ev.as_payload()

    assert wire["event_id"] == str(d.event_id)
    assert wire["occurred_at"] == d.occurred_at.isoformat()
    assert wire["agent_name"] == "researcher"
    assert wire["tool_name"] == "read_file"
    assert wire["outcome"] == "deny"
    assert wire["role"] == "analyst"
    assert wire["error_type"] == "policy_denied"
    assert wire["reason"] == "denied for path"
    assert wire["violations"] == ["v1", "v2"]
    assert wire["hint"] == {"glob": "/x/**"}
    assert wire["arguments"] == {"path": "/etc/passwd"}
    assert wire["user_id"] == "alice"
    assert wire["session_id"] == "sess_1"


def test_as_payload_server_resolved_fields_absent() -> None:
    """project_id, agent_version_id, received_at are server-resolved or server-stamped."""
    wire = AuditEvent(decision=_decision()).as_payload()
    assert "project_id" not in wire
    assert "agent_version_id" not in wire
    assert "received_at" not in wire


def test_as_payload_none_normalizes_to_empty_string() -> None:
    d = _decision(role=None, error_type=None)
    wire = AuditEvent(decision=d).as_payload()  # user_id/session_id default to ""
    assert wire["role"] == ""
    assert wire["error_type"] == ""
    assert wire["user_id"] == ""
    assert wire["session_id"] == ""


def test_as_payload_violations_tuple_serializes_as_list() -> None:
    """Decision.violations is tuple[str, ...] but the wire payload is a list."""
    wire = AuditEvent(decision=_decision(violations=("a", "b", "c"))).as_payload()
    assert wire["violations"] == ["a", "b", "c"]
    assert isinstance(wire["violations"], list)


def test_event_id_and_occurred_at_unique_per_decision() -> None:
    w1 = AuditEvent(decision=_decision()).as_payload()
    w2 = AuditEvent(decision=_decision()).as_payload()
    assert w1["event_id"] != w2["event_id"]
    assert "+00:00" in w1["occurred_at"]
