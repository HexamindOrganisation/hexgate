"""Tests for /v1/audit/decisions and the audit Pydantic models.

Two tiers:
  * Pydantic + endpoint tests run with the ClickHouse client and the auth
    dependency stubbed via FastAPI ``dependency_overrides``. Fast, offline,
    no Docker needed.
  * Integration tests carry the ``@pytest.mark.integration`` marker, skipped
    by default; opt in with ``pytest -m integration`` once
    ``make clickhouse-up`` is running.

The bearer-resolution path itself is exercised by ``test_biscuits.py`` and
``test_keystore.py``; this module focuses on the audit handler's own
validation, byte caps, ClickHouse interaction, and response shape.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from clickhouse_connect.driver.exceptions import ClickHouseError
from fastapi.testclient import TestClient
from pydantic import ValidationError

import audit
from main import app, get_clickhouse, require_project
from schemas import DecisionEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(**overrides) -> dict:
    """Return a minimal-required event payload, with optional overrides."""
    base = {
        "event_id":    str(uuid.uuid4()),
        "occurred_at": _now().isoformat(),
        "agent_name":  "researcher",
        "tool_name":   "read_file",
        "outcome":     "deny",
    }
    return {**base, **overrides}


# ---------------------------------------------------------------------------
# Pydantic schema validation
# ---------------------------------------------------------------------------


def test_minimal_event_constructs_with_envelope_defaults() -> None:
    e = DecisionEvent(**_event())
    # Envelope defaults
    assert e.session_id == ""
    assert e.user_id == ""
    assert e.agent_version_id == ""
    # Decision-detail defaults
    assert e.role == ""
    assert e.error_type == ""
    assert e.violations == []
    assert e.hint is None
    assert e.arguments is None


def test_envelope_fields_inherited_via_mixin() -> None:
    """DecisionEvent inherits the full envelope from AuditEnvelope.

    Mirrors the envelope prefix of platform/clickhouse/init/schema.sql.
    project_id and received_at are intentionally absent — server-resolved
    and server-stamped respectively, never trusted from the body.
    """
    expected = {
        "event_id", "occurred_at", "agent_name",
        "agent_version_id", "session_id", "user_id",
    }
    assert expected <= DecisionEvent.model_fields.keys()
    # And the inverse: server-managed fields stay out of the wire model.
    assert "project_id" not in DecisionEvent.model_fields
    assert "received_at" not in DecisionEvent.model_fields


def test_bad_outcome_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        DecisionEvent(**_event(outcome="maybe"))
    assert "outcome" in str(exc.value)


def test_missing_required_field_rejected() -> None:
    payload = _event()
    payload.pop("tool_name")
    with pytest.raises(ValidationError) as exc:
        DecisionEvent(**payload)
    assert "tool_name" in str(exc.value)


def test_oversized_agent_name_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        DecisionEvent(**_event(agent_name="x" * 300))
    assert "agent_name" in str(exc.value)


# ---------------------------------------------------------------------------
# Endpoint behaviour — auth + ClickHouse stubbed
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_clickhouse() -> MagicMock:
    """A MagicMock standing in for the ClickHouse client.

    Tests inspect ``.insert.call_args`` to assert what the handler wrote,
    and set ``.insert.side_effect`` to simulate failures.
    """
    return MagicMock()


@pytest.fixture
def client(fake_clickhouse: MagicMock) -> TestClient:
    """TestClient with require_project + get_clickhouse stubbed.

    ``require_project`` returns the literal ``proj_test`` so assertions
    on the server-stamped ``project_id`` row value stay deterministic.
    """
    app.dependency_overrides[require_project] = lambda: "proj_test"
    app.dependency_overrides[get_clickhouse] = lambda: fake_clickhouse
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_happy_path_returns_202_and_inserts_row(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    payload = _event()
    r = client.post("/v1/audit/decisions", json=payload)

    assert r.status_code == 202, r.text
    assert r.json() == {"event_id": payload["event_id"]}

    fake_clickhouse.insert.assert_called_once()
    args, kwargs = fake_clickhouse.insert.call_args
    assert args[0] == "policy_decision"
    rows = args[1]
    assert len(rows) == 1
    assert len(rows[0]) == 15  # column count matches schema (received_at absent)
    # project_id stamped from bearer (override returned "proj_test"), index 2
    assert rows[0][2] == "proj_test"
    assert kwargs["column_names"] == audit._DECISION_COLUMNS
    assert kwargs["settings"]["async_insert"] == 1
    assert kwargs["settings"]["wait_for_async_insert"] == 0


def test_future_occurred_at_rejected(client: TestClient) -> None:
    far_future = (_now() + timedelta(minutes=10)).isoformat()
    r = client.post("/v1/audit/decisions", json=_event(occurred_at=far_future))
    assert r.status_code == 400
    assert "future" in r.json()["detail"]


def test_too_old_occurred_at_rejected(client: TestClient) -> None:
    too_old = (_now() - timedelta(days=91)).isoformat()
    r = client.post("/v1/audit/decisions", json=_event(occurred_at=too_old))
    assert r.status_code == 400
    assert "retention" in r.json()["detail"]


def test_oversized_arguments_rejected(client: TestClient) -> None:
    big = {"key": "x" * (audit.MAX_ARGS_BYTES + 100)}
    r = client.post("/v1/audit/decisions", json=_event(arguments=big))
    assert r.status_code == 413
    assert "arguments" in r.json()["detail"]


def test_oversized_hint_rejected(client: TestClient) -> None:
    big = {"globs": "y" * (audit.MAX_HINT_BYTES + 100)}
    r = client.post("/v1/audit/decisions", json=_event(hint=big))
    assert r.status_code == 413
    assert "hint" in r.json()["detail"]


def test_pydantic_validation_returns_422(client: TestClient) -> None:
    """Bad outcome trips Pydantic before our handler runs."""
    r = client.post("/v1/audit/decisions", json=_event(outcome="maybe"))
    assert r.status_code == 422


def test_clickhouse_error_returns_503_with_retry_after(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    fake_clickhouse.insert.side_effect = ClickHouseError("connection refused")
    r = client.post("/v1/audit/decisions", json=_event())
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "5"
    assert "unavailable" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Integration — requires `make clickhouse-up` first; opt-in via marker
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_real_clickhouse_round_trip() -> None:
    """Insert a row through the real client; SELECT it back; assert shape.

    Uses a uniquely-tagged ``project_id`` so concurrent runs don't collide,
    and cleans up its own rows on the way out.
    """
    from clickhouse import get_clickhouse as real_get_clickhouse

    ch = real_get_clickhouse()
    event_id = uuid.uuid4()
    project_id = f"test_proj_{uuid.uuid4().hex[:8]}"

    ch.insert(
        "policy_decision",
        [[
            event_id,
            _now(),
            project_id,
            "researcher",
            "9f1e3c5a-test",   # agent_version_id
            "sess_test",
            "u_test",
            "read_file",
            "analyst",
            "deny",
            "policy_denied",
            "integration test row",
            ["v1"],
            json.dumps({"glob": "/workspace/**"}),
            json.dumps({"path": "/etc/passwd"}),
        ]],
        column_names=audit._DECISION_COLUMNS,
        # wait_for_async_insert=1 so the row is queryable immediately on
        # the SELECT below — otherwise we'd race the server-side buffer.
        settings={"async_insert": 1, "wait_for_async_insert": 1},
    )

    try:
        rows = ch.query(
            "SELECT event_id, project_id, outcome, received_at, agent_version_id "
            "FROM policy_decision WHERE project_id = {pid:String}",
            parameters={"pid": project_id},
        ).result_rows
        assert len(rows) == 1
        ev_id, pid, outcome, received_at, av_id = rows[0]
        assert str(ev_id) == str(event_id)
        assert pid == project_id
        assert outcome == "deny"
        assert received_at is not None  # server-stamped via column default
        assert av_id == "9f1e3c5a-test"
    finally:
        ch.command(
            "ALTER TABLE policy_decision DELETE WHERE project_id = {pid:String}",
            parameters={"pid": project_id},
        )
