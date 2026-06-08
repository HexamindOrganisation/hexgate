"""Tests for /v1/audit/decisions and the audit Pydantic models.

Endpoint tests stub auth + ClickHouse via dependency_overrides. Integration
tests under @pytest.mark.integration round-trip against a real local ClickHouse.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from clickhouse_connect.driver.exceptions import (
    ClickHouseError,
    DataError,
    OperationalError,
)
from fastapi.testclient import TestClient
from pydantic import ValidationError

import audit
import main
from audit import list_decisions, summarize
from keystore import FileKeyStore
from main import (
    app,
    get_session,
    require_clickhouse,
    require_org_member,
    require_project,
    require_user,
)
from schemas import DecisionEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(**overrides) -> dict:
    """Return a minimal-required event payload, with optional overrides."""
    base = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": _now().isoformat(),
        "agent_name": "researcher",
        "tool_name": "read_file",
        "outcome": "deny",
    }
    return {**base, **overrides}


# ---------------------------------------------------------------------------
# Pydantic schema validation
# ---------------------------------------------------------------------------


def test_minimal_event_constructs_with_envelope_defaults() -> None:
    e = DecisionEvent(**_event())
    # Envelope defaults (agent_version_id is server-resolved, not in the wire model)
    assert e.session_id == ""
    assert e.user_id == ""
    # Decision-detail defaults
    assert e.role == ""
    assert e.error_type == ""
    assert e.violations == []
    assert e.hint is None
    assert e.arguments is None


def test_envelope_fields_inherited_via_mixin() -> None:
    """DecisionEvent inherits the wire envelope; server-resolved fields stay out."""
    expected = {"event_id", "occurred_at", "agent_name", "session_id", "user_id"}
    assert expected <= DecisionEvent.model_fields.keys()
    assert "project_id" not in DecisionEvent.model_fields
    assert "received_at" not in DecisionEvent.model_fields
    assert "agent_version_id" not in DecisionEvent.model_fields


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
    """MagicMock for the ClickHouse client."""
    return MagicMock()


# Stub return value for the agent_version_id lookup; tests assert it lands in the row.
_STUB_AGENT_VERSION_ID = "stub_v_id_xyz"


@pytest.fixture
def client(
    fake_clickhouse: MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> TestClient:
    """TestClient with auth, ClickHouse, session, and version-lookup stubbed."""
    app.dependency_overrides[require_project] = lambda: "proj_test"
    app.dependency_overrides[require_clickhouse] = lambda: fake_clickhouse
    app.dependency_overrides[get_session] = lambda: MagicMock()

    async def _stub_version_lookup(_session, _project_id, _agent_name) -> str:
        return _STUB_AGENT_VERSION_ID

    monkeypatch.setattr("main.get_latest_agent_version_id", _stub_version_lookup)
    # The dashboard-read gating tests run the real require_org_member chain,
    # whose cookie transport needs an initialised keystore (same swap as the
    # client fixture in test_auth.py).
    original_keystore = main.keystore
    main.keystore = FileKeyStore(base_dir=tmp_path / "keystore")
    main.keystore.ensure_keypair()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        main.keystore = original_keystore


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
    assert len(rows[0]) == 15
    # Indices match _DECISION_COLUMNS in audit.py.
    assert rows[0][2] == "proj_test"  # project_id (bearer)
    assert rows[0][4] == _STUB_AGENT_VERSION_ID  # agent_version_id (platform)
    assert kwargs["column_names"] == audit._DECISION_COLUMNS
    assert kwargs["settings"]["async_insert"] == 1
    # Durable: block until flush so insert failures surface synchronously.
    assert kwargs["settings"]["wait_for_async_insert"] == 1


def test_agent_version_id_comes_from_platform_lookup(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    """Even if the SDK sneaks agent_version_id into the body, the platform lookup wins."""
    payload = {**_event(), "agent_version_id": "sdk_provided_should_be_ignored"}
    r = client.post("/v1/audit/decisions", json=payload)
    assert r.status_code == 202

    rows = fake_clickhouse.insert.call_args.args[1]
    assert rows[0][4] == _STUB_AGENT_VERSION_ID
    assert "sdk_provided_should_be_ignored" not in rows[0]


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


def test_oversized_violation_item_rejected(client: TestClient) -> None:
    # Item count is capped at 64, but each item must also be bounded —
    # otherwise 64 unbounded strings get a multi-MB body past validation.
    r = client.post("/v1/audit/decisions", json=_event(violations=["z" * 2048]))
    assert r.status_code == 422


def test_pydantic_validation_returns_422(client: TestClient) -> None:
    """Bad outcome trips Pydantic before the handler runs."""
    r = client.post("/v1/audit/decisions", json=_event(outcome="maybe"))
    assert r.status_code == 422


def test_transient_clickhouse_error_returns_503_with_retry_after(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    """A transport/transient failure is retryable → 503 Retry-After."""
    fake_clickhouse.insert.side_effect = OperationalError("connection refused")
    r = client.post("/v1/audit/decisions", json=_event())
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "5"
    assert "unavailable" in r.json()["detail"]


def test_deterministic_clickhouse_error_returns_422(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    """A storage rejection (bad type/value) is permanent → 422, not a retryable 503."""
    fake_clickhouse.insert.side_effect = DataError("unknown enum value")
    r = client.post("/v1/audit/decisions", json=_event())
    assert r.status_code == 422
    assert "retry-after" not in {k.lower() for k in r.headers}
    assert "rejected" in r.json()["detail"]


def test_naive_occurred_at_accepted_as_utc(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    """A timezone-naive occurred_at is treated as UTC, not a 500 from the skew check."""
    naive = _now().replace(tzinfo=None).isoformat()  # UTC wall-clock, no offset/Z
    r = client.post("/v1/audit/decisions", json=_event(occurred_at=naive))
    assert r.status_code == 202, r.text
    fake_clickhouse.insert.assert_called_once()
    # occurred_at lands tz-aware in the row (index 1 per _DECISION_COLUMNS).
    stored = fake_clickhouse.insert.call_args.args[1][0][1]
    assert stored.tzinfo is not None


def test_clickhouse_unreachable_at_connect_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_clickhouse() raising during dependency resolution maps to 503, not 500."""
    from fastapi import HTTPException

    from main import require_clickhouse

    def _boom() -> None:
        raise ClickHouseError("connection refused")

    monkeypatch.setattr("main.get_clickhouse", _boom)
    with pytest.raises(HTTPException) as exc:
        require_clickhouse()
    assert exc.value.status_code == 503
    assert exc.value.headers.get("Retry-After") == "5"


# ---------------------------------------------------------------------------
# _scope() — WHERE-clause + params composition
# ---------------------------------------------------------------------------

_BASE_WHERE = [
    "project_id = {pid:String}",
    "occurred_at >= now() - INTERVAL {hrs:UInt32} HOUR",
]


def test_scope_no_filters() -> None:
    where, params = audit._scope("p1", 24)
    assert where == _BASE_WHERE
    assert params == {"pid": "p1", "hrs": 24}


def test_scope_agent_only() -> None:
    where, params = audit._scope("p1", 24, agent="researcher")
    assert where == _BASE_WHERE + ["agent_name = {agent:String}"]
    assert params == {"pid": "p1", "hrs": 24, "agent": "researcher"}


def test_scope_role_only() -> None:
    where, params = audit._scope("p1", 168, role="analyst")
    assert where == _BASE_WHERE + ["role = {role:String}"]
    assert params == {"pid": "p1", "hrs": 168, "role": "analyst"}


def test_scope_empty_role_filters_no_role_bucket() -> None:
    """role="" (the dashboard's "(none)" drill-down) must still emit the
    role clause — `if role:` instead of `if role is not None:` would
    silently widen the filter to every role."""
    where, params = audit._scope("p1", 24, role="")
    assert "role = {role:String}" in where
    assert params["role"] == ""


def test_scope_all_filters() -> None:
    where, params = audit._scope(
        "p1", 720, agent="researcher", role="analyst", tool="read_file"
    )
    assert where == _BASE_WHERE + [
        "agent_name = {agent:String}",
        "role = {role:String}",
        "tool_name = {tool:String}",
    ]
    assert params == {
        "pid": "p1",
        "hrs": 720,
        "agent": "researcher",
        "role": "analyst",
        "tool": "read_file",
    }
    # Every dynamic value travels as a bound parameter, never spliced into
    # the SQL string — the injection-shape invariant for this module.
    assert all("{" in clause and ":" in clause for clause in where)


# ---------------------------------------------------------------------------
# summarize() — GROUPING SETS row classification
# ---------------------------------------------------------------------------

# Rows are (agent, role, tool, outcome, g_agent, g_role, g_tool, g_outcome, n).
# GROUPING() flags: 1 = column rolled up. Only the () set rolls up outcome.


def _summary_result(rows: list[tuple]) -> MagicMock:
    client = MagicMock()
    client.query.return_value.result_rows = rows
    return client


def test_summarize_classifies_grouping_sets() -> None:
    client = _summary_result(
        [
            # () — grand total (the ONLY row where g_outcome=1)
            ("", "", "", "", 1, 1, 1, 1, 10),
            # (outcome) — per-outcome totals
            ("", "", "", "allow", 1, 1, 1, 0, 6),
            ("", "", "", "deny", 1, 1, 1, 0, 4),
            # (agent_name, outcome)
            ("researcher", "", "", "allow", 0, 1, 1, 0, 6),
            ("researcher", "", "", "deny", 0, 1, 1, 0, 3),
            ("scraper", "", "", "deny", 0, 1, 1, 0, 1),
            # (role, outcome) — empty role keeps its raw "" key on the wire
            ("", "analyst", "", "allow", 1, 0, 1, 0, 6),
            ("", "", "", "deny", 1, 0, 1, 0, 4),
            # (tool_name, outcome)
            ("", "", "read_file", "deny", 1, 1, 0, 0, 4),
        ]
    )

    data = summarize(client, project_id="p1", since_hours=24)

    assert data["totals"] == {
        "all": 10,
        "allow": 6,
        "deny": 4,
        "needs_approval": 0,
    }
    # Breakdowns sorted by "all" desc; grand total must NOT leak into any.
    assert data["by_agent"] == [
        {"key": "researcher", "all": 9, "allow": 6, "deny": 3, "needs_approval": 0},
        {"key": "scraper", "all": 1, "allow": 0, "deny": 1, "needs_approval": 0},
    ]
    assert data["by_role"] == [
        {"key": "analyst", "all": 6, "allow": 6, "deny": 0, "needs_approval": 0},
        {"key": "", "all": 4, "allow": 0, "deny": 4, "needs_approval": 0},
    ]
    assert data["by_tool"] == [
        {"key": "read_file", "all": 4, "allow": 0, "deny": 4, "needs_approval": 0},
    ]


def test_summarize_empty_result() -> None:
    data = summarize(_summary_result([]), project_id="p1", since_hours=24)
    assert data == {
        "totals": {"all": 0, "allow": 0, "deny": 0, "needs_approval": 0},
        "by_agent": [],
        "by_role": [],
        "by_tool": [],
    }


# ---------------------------------------------------------------------------
# list_decisions() — count() OVER () pagination contract
# ---------------------------------------------------------------------------

_LIST_COLUMN_NAMES = [c.strip() for c in audit._LIST_COLUMNS.split(",")] + [
    "total_matches"
]


def _decision_row(total: int, **overrides) -> tuple:
    base = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": _now(),
        "received_at": _now(),
        "agent_name": "researcher",
        "agent_version_id": "v1",
        "session_id": "sess_1",
        "user_id": "u_1",
        "tool_name": "read_file",
        "role": "",
        "outcome": "deny",
        "error_type": "policy_denied",
        "reason": "",
        "violations": ["v1"],
        "hint": '{"globs": "/workspace/**"}',
        "arguments": "",
        "total_matches": total,
    }
    base.update(overrides)
    return tuple(base[c] for c in _LIST_COLUMN_NAMES)


def test_list_decisions_total_from_window_function() -> None:
    """An in-range page carries total via count() OVER () — one scan, no
    second count() query."""
    client = MagicMock()
    client.query.return_value.result_rows = [_decision_row(3), _decision_row(3)]
    client.query.return_value.column_names = _LIST_COLUMN_NAMES

    page = list_decisions(client, project_id="p1", since_hours=24, limit=2, offset=0)

    assert page["total"] == 3
    assert page["limit"] == 2 and page["offset"] == 0
    assert len(page["rows"]) == 2
    client.query.assert_called_once()
    # JSON columns decode; "" → None.
    assert page["rows"][0]["hint"] == {"globs": "/workspace/**"}
    assert page["rows"][0]["arguments"] is None
    assert "total_matches" not in page["rows"][0]


def test_list_decisions_past_end_page_falls_back_to_count() -> None:
    """A page past the end (offset > 0, zero rows) has no window value to
    read total from → the separate count() branch supplies it."""
    page_result = MagicMock()
    page_result.result_rows = []
    page_result.column_names = _LIST_COLUMN_NAMES
    count_result = MagicMock()
    count_result.result_rows = [[3]]
    client = MagicMock()
    client.query.side_effect = [page_result, count_result]

    page = list_decisions(client, project_id="p1", since_hours=24, limit=25, offset=75)

    assert page["rows"] == []
    assert page["total"] == 3
    assert client.query.call_count == 2
    assert "count()" in client.query.call_args_list[1].args[0]


def test_list_decisions_empty_first_page_skips_count() -> None:
    """offset=0 with no rows means a genuinely empty slice — total is 0
    and the fallback count() must not fire."""
    client = MagicMock()
    client.query.return_value.result_rows = []
    client.query.return_value.column_names = _LIST_COLUMN_NAMES

    page = list_decisions(client, project_id="p1", since_hours=24)

    assert page["total"] == 0 and page["rows"] == []
    client.query.assert_called_once()


# ---------------------------------------------------------------------------
# Dashboard read endpoints — require_org_member gating
# ---------------------------------------------------------------------------

# All three project-scoped reads share one trust envelope (require_org_member,
# same as the other dashboard reads); membership semantics (403 non-member,
# 404 unknown project) are covered against a real DB in test_auth.py.
_AUDIT_READ_PATHS = [
    "/v1/projects/proj_test/audit/summary",
    "/v1/projects/proj_test/audit/timeseries",
    "/v1/projects/proj_test/audit/decisions",
]


@pytest.mark.parametrize("path", _AUDIT_READ_PATHS)
def test_audit_read_rejects_anonymous(
    client: TestClient, fake_clickhouse: MagicMock, path: str
) -> None:
    """No cookie / dev header → the require_org_member chain 401s before
    the handler runs, so ClickHouse is never queried."""
    r = client.get(path)
    assert r.status_code == 401
    fake_clickhouse.query.assert_not_called()


@pytest.mark.parametrize("path", _AUDIT_READ_PATHS)
def test_audit_read_allows_org_member(
    client: TestClient, fake_clickhouse: MagicMock, path: str
) -> None:
    """With membership satisfied, the same request reaches the handler —
    proving the 401 above comes from the auth gate, not the route."""
    app.dependency_overrides[require_org_member] = lambda: MagicMock()
    fake_clickhouse.query.return_value.result_rows = []
    r = client.get(path)
    assert r.status_code == 200, r.text


class _FakeAuthSession:
    """Just enough async-session surface for require_org_member: ``get``
    resolves the project, ``exec(...).first()`` resolves the membership."""

    def __init__(self, project, membership) -> None:
        self._project = project
        self._membership = membership

    async def get(self, _model, _pk):
        return self._project

    async def exec(self, _stmt):
        result = MagicMock()
        result.first.return_value = self._membership
        return result


def _login_as_stub_user(project, membership) -> None:
    """Authenticate as a stub user and point require_org_member's DB
    lookups at canned project/membership values."""
    app.dependency_overrides[require_user] = lambda: MagicMock()
    app.dependency_overrides[get_session] = lambda: _FakeAuthSession(
        project, membership
    )


@pytest.mark.parametrize("path", _AUDIT_READ_PATHS)
def test_audit_read_unknown_project_is_404(
    client: TestClient, fake_clickhouse: MagicMock, path: str
) -> None:
    """Authenticated but the project doesn't exist → 404, so project IDs
    can't be enumerated via 403-vs-404 differences."""
    _login_as_stub_user(project=None, membership=None)
    r = client.get(path)
    assert r.status_code == 404
    fake_clickhouse.query.assert_not_called()


@pytest.mark.parametrize("path", _AUDIT_READ_PATHS)
def test_audit_read_non_member_is_403(
    client: TestClient, fake_clickhouse: MagicMock, path: str
) -> None:
    """Authenticated, project exists, but the user isn't in its org → 403."""
    _login_as_stub_user(project=MagicMock(org_id="org_other"), membership=None)
    r = client.get(path)
    assert r.status_code == 403
    fake_clickhouse.query.assert_not_called()


def test_audit_read_empty_role_param_filters_no_role_bucket(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    """``role=`` (empty value) must reach ClickHouse as ``role = ''`` —
    the no-role drill-down — while an absent ``role`` means no filter.
    No "(none)" sentinel exists on the wire."""
    app.dependency_overrides[require_org_member] = lambda: MagicMock()
    fake_clickhouse.query.return_value.result_rows = []

    r = client.get("/v1/projects/proj_test/audit/summary?role=")
    assert r.status_code == 200, r.text
    params = fake_clickhouse.query.call_args.kwargs["parameters"]
    assert params["role"] == ""

    fake_clickhouse.query.reset_mock()
    r = client.get("/v1/projects/proj_test/audit/summary")
    assert r.status_code == 200, r.text
    params = fake_clickhouse.query.call_args.kwargs["parameters"]
    assert "role" not in params


@pytest.mark.parametrize("path", _AUDIT_READ_PATHS)
def test_audit_read_member_is_200(
    client: TestClient, fake_clickhouse: MagicMock, path: str
) -> None:
    """Authenticated + membership row present → the real require_org_member
    passes and the handler answers."""
    _login_as_stub_user(project=MagicMock(org_id="org_1"), membership=MagicMock())
    fake_clickhouse.query.return_value.result_rows = []
    r = client.get(path)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Health (liveness) vs readiness split
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/health", "/v1/health"])
def test_liveness_does_not_ping_clickhouse(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Liveness must not touch ClickHouse, so an outage can't cascade into restarts."""

    def _fail() -> bool:
        raise AssertionError("liveness probe must not ping ClickHouse")

    monkeypatch.setattr("main.clickhouse_ping", _fail)
    r = TestClient(app).get(path)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "clickhouse" not in r.json()


@pytest.mark.parametrize("path", ["/ready", "/v1/ready"])
def test_readiness_reports_clickhouse(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr("main.clickhouse_ping", lambda: False)
    r = TestClient(app).get(path)
    assert r.status_code == 503
    assert r.json()["clickhouse"] == "unreachable"


# ---------------------------------------------------------------------------
# Integration — requires `make clickhouse-up` first; opt-in via marker
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_real_clickhouse_round_trip() -> None:
    """Insert through the real write path (``insert_decision`` with
    ``_DECISION_INSERT_SETTINGS``); SELECT it back; clean up."""
    from audit import insert_decision
    from clickhouse import get_clickhouse as real_get_clickhouse

    clickhouse_client = real_get_clickhouse()
    # The shared client is sessionless (autogenerate_session_id=False in
    # clickhouse.py) — a session would reject the concurrent queries the
    # dashboard reads + SDK ingest fire at the same pool.
    assert "session_id" not in clickhouse_client.params

    project_id = f"test_proj_{uuid.uuid4().hex[:8]}"
    event = DecisionEvent(
        **_event(
            session_id="sess_test",
            user_id="u_test",
            role="analyst",
            error_type="policy_denied",
            reason="integration test row",
            violations=["v1"],
            hint={"glob": "/workspace/**"},
            arguments={"path": "/etc/passwd"},
        )
    )
    event_id = event.event_id

    # wait_for_async_insert=1 (in _DECISION_INSERT_SETTINGS) blocks until the
    # flush — returning without raising IS the ack on the sessionless client.
    insert_decision(
        clickhouse_client,
        event=event,
        project_id=project_id,
        agent_version_id="9f1e3c5a-test",
    )

    try:
        rows = clickhouse_client.query(
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
        clickhouse_client.command(
            "ALTER TABLE policy_decision DELETE WHERE project_id = {pid:String}",
            parameters={"pid": project_id},
        )
