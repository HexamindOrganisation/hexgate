"""features/llm_messages — the session-scoped transcript read: list_llm_messages()
and the GET /v1/projects/{p}/audit/llm-messages endpoint it backs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from clickhouse_connect.driver.exceptions import OperationalError
from fastapi.testclient import TestClient

from hexgate_api.core import keystore as keystore_mod
from hexgate_api.core.clickhouse import ZERO_RUN_ID, BatchItem
from hexgate_api.core.db import get_session
from hexgate_api.core.keystore import FileKeyStore
from hexgate_api.deps.clickhouse import require_clickhouse
from hexgate_api.deps.identity import require_user
from hexgate_api.deps.org import require_org_member
from hexgate_api.features.llm_messages.service import (
    MAX_PAGE_SIZE,
    NoMessageScope,
    insert_llm_messages_batch,
    list_llm_messages,
)
from hexgate_api.main import app
from hexgate_api.schemas import LlmMessageEvent
from hexgate_api.query_scope import RETENTION_WINDOW

# ---------------------------------------------------------------------------
# Helpers — a fake ClickHouse whose query() returns canned transcript rows
# ---------------------------------------------------------------------------

_LIST_COLUMN_NAMES = [
    "event_id",
    "occurred_at",
    "received_at",
    "agent_name",
    "agent_version_id",
    "session_id",
    "user_id",
    "model",
    "turn_key",
    "message_seq",
    "resynced",
    "truncated",
    "input_messages",
    "output_messages",
    "system_instructions",
    "run_id",
    "total_matches",
]

_INPUT = json.dumps([{"role": "user", "parts": [{"type": "text", "content": "hi"}]}])
_OUTPUT = json.dumps([{"role": "assistant", "parts": []}])


def _stored_row(**overrides) -> list:
    """One ClickHouse result row in _LIST_COLUMN_NAMES order."""
    base = {
        "event_id": uuid.uuid4(),
        "occurred_at": datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc),
        "received_at": datetime(2026, 9, 14, 10, 0, 1, tzinfo=timezone.utc),
        "agent_name": "researcher",
        "agent_version_id": "ver_1",
        "session_id": "sess_1",
        "user_id": "u1",
        "model": "gpt-4o",
        "turn_key": "run_1:researcher",
        "message_seq": 0,
        "resynced": 0,
        "truncated": 0,
        "input_messages": _INPUT,
        "output_messages": _OUTPUT,
        "system_instructions": "",
        "run_id": ZERO_RUN_ID,
        "total_matches": 1,
    }
    base.update(overrides)
    return [base[name] for name in _LIST_COLUMN_NAMES]


def _client_returning(*rows: list) -> MagicMock:
    client = MagicMock()
    client.query.return_value.result_rows = list(rows)
    client.query.return_value.column_names = _LIST_COLUMN_NAMES
    return client


# ---------------------------------------------------------------------------
# list_llm_messages()
# ---------------------------------------------------------------------------


def test_list_llm_messages_happy_path() -> None:
    """One scan returns the page and its unpaginated total, with the JSON
    content columns decoded back to objects."""
    client = _client_returning(_stored_row(total_matches=3))

    page = list_llm_messages(client, project_id="p1", session_id="sess_1")

    assert page["total"] == 3
    assert page["limit"] == 50 and page["offset"] == 0
    (row,) = page["rows"]
    assert row["input_messages"] == json.loads(_INPUT)
    assert row["output_messages"] == json.loads(_OUTPUT)
    assert row["system_instructions"] is None
    assert row["turn_key"] == "run_1:researcher"
    client.query.assert_called_once()


def test_list_llm_messages_scopes_to_project_and_session() -> None:
    """Both the tenant and the session are bound parameters, never
    interpolated — the read is a tenant boundary, not just a filter."""
    client = _client_returning()

    list_llm_messages(client, project_id="p1", session_id="sess_1")

    sql = client.query.call_args.args[0]
    params = client.query.call_args.kwargs["parameters"]
    assert "project_id = {pid:String}" in sql
    assert "session_id = {session_id:String}" in sql
    assert params["pid"] == "p1" and params["session_id"] == "sess_1"
    assert "run_id" not in params


def test_when_only_a_run_id_is_given_then_it_scopes_the_read() -> None:
    """The common case: the SDK user never set a session id, so the rows
    carry session_id='' and run_id is the only scope left."""
    client = _client_returning()
    run_id = uuid.uuid4()

    list_llm_messages(client, project_id="p1", run_id=run_id)

    sql = client.query.call_args.args[0]
    params = client.query.call_args.kwargs["parameters"]
    assert "run_id = {run_id:UUID}" in sql
    assert params["run_id"] == run_id
    assert "session_id" not in params


def test_when_both_scopes_are_given_then_both_are_applied() -> None:
    """A caller holding both means the intersection; there is no reading
    under which naming a second scope should widen the result."""
    client = _client_returning()
    run_id = uuid.uuid4()

    list_llm_messages(client, project_id="p1", session_id="sess_1", run_id=run_id)

    sql = client.query.call_args.args[0]
    assert "session_id = {session_id:String}" in sql
    assert "run_id = {run_id:UUID}" in sql


def test_when_no_scope_is_given_then_it_raises() -> None:
    """An unscoped read would stream the project's entire message history,
    so it must never reach ClickHouse."""
    client = _client_returning()

    with pytest.raises(NoMessageScope):
        list_llm_messages(client, project_id="p1")

    client.query.assert_not_called()


def test_when_the_run_id_is_the_zero_uuid_then_it_is_not_a_scope() -> None:
    """The zero UUID is the column's "outside any run" value, shared by every
    unattributed row in the project — it names no transcript."""
    client = _client_returning()

    with pytest.raises(NoMessageScope):
        list_llm_messages(client, project_id="p1", run_id=ZERO_RUN_ID)

    client.query.assert_not_called()


def test_list_llm_messages_orders_oldest_first() -> None:
    """A transcript reads forwards, and message_seq/event_id complete the
    total order paging needs — matching the storage sort key."""
    client = _client_returning()

    list_llm_messages(client, project_id="p1", session_id="sess_1")

    sql = client.query.call_args.args[0]
    assert "ORDER BY occurred_at, message_seq, event_id" in sql
    assert "DESC" not in sql


def test_when_row_has_a_real_run_id_then_it_is_returned() -> None:
    run_id = uuid.uuid4()
    client = _client_returning(_stored_row(run_id=run_id))

    (row,) = list_llm_messages(client, project_id="p1", session_id="sess_1")["rows"]

    assert row["run_id"] == run_id


def test_when_row_has_the_zero_run_id_then_run_id_is_none() -> None:
    """The column's "outside a run" value must not surface as a run id that
    joins to no policy_decision row."""
    client = _client_returning(_stored_row(run_id=ZERO_RUN_ID))

    (row,) = list_llm_messages(client, project_id="p1", session_id="sess_1")["rows"]

    assert row["run_id"] is None


def test_when_flags_are_set_then_the_stored_values_reach_the_row() -> None:
    """The UInt8 flags pass through untouched — the reader marks restated
    history and lossy content from them. The int->bool step is the response
    model's, covered by test_llm_messages_read_returns_the_page."""
    client = _client_returning(_stored_row(resynced=1, truncated=0))

    (row,) = list_llm_messages(client, project_id="p1", session_id="sess_1")["rows"]

    assert row["resynced"] == 1 and row["truncated"] == 0


def test_when_content_is_not_json_then_it_is_returned_as_text() -> None:
    """A row written before a serialization change still reads back."""
    client = _client_returning(_stored_row(input_messages="{not json"))

    (row,) = list_llm_messages(client, project_id="p1", session_id="sess_1")["rows"]

    assert row["input_messages"] == "{not json"


def test_when_page_past_the_end_then_total_falls_back_to_count() -> None:
    """An empty page at offset > 0 carries no count() OVER () value, so the
    total comes from a second plain count."""
    client = MagicMock()
    client.query.side_effect = [
        MagicMock(result_rows=[], column_names=_LIST_COLUMN_NAMES),
        MagicMock(result_rows=[[7]]),
    ]

    page = list_llm_messages(client, project_id="p1", session_id="sess_1", offset=50)

    assert page["total"] == 7 and page["rows"] == []
    assert client.query.call_count == 2


def test_when_first_page_is_empty_then_the_fallback_count_is_skipped() -> None:
    """offset=0 with no rows is a genuinely empty session — total is 0."""
    client = _client_returning()

    page = list_llm_messages(client, project_id="p1", session_id="sess_1")

    assert page["total"] == 0 and page["rows"] == []
    client.query.assert_called_once()


def test_list_llm_messages_scopes_to_the_full_retention_window() -> None:
    """The session is the scope, so the only time bound is the horizon past
    which nothing is stored. A dashboard-sized window would silently cut the
    head off a conversation that started before it, and the surviving rows
    would begin at a nonzero message_seq — which reads as a lost row."""
    client = _client_returning()
    before = datetime.now(timezone.utc)

    list_llm_messages(client, project_id="p1", session_id="sess_1")

    since = client.query.call_args.kwargs["parameters"]["since"]
    assert before - RETENTION_WINDOW - timedelta(minutes=1) <= since
    assert since <= datetime.now(timezone.utc) - RETENTION_WINDOW + timedelta(minutes=1)


# ---------------------------------------------------------------------------
# Endpoint — auth + ClickHouse stubbed
# ---------------------------------------------------------------------------

_READ_PATH = "/v1/projects/proj_test/audit/llm-messages"
_READ_URL = f"{_READ_PATH}?session_id=sess_1"


@pytest.fixture
def fake_clickhouse() -> MagicMock:
    client = MagicMock()
    client.query.return_value.result_rows = []
    client.query.return_value.column_names = _LIST_COLUMN_NAMES
    return client


@pytest.fixture
def client(fake_clickhouse: MagicMock, tmp_path) -> TestClient:
    """TestClient with ClickHouse stubbed; auth runs the real
    require_org_member chain, whose cookie transport needs a keystore."""
    app.dependency_overrides[require_clickhouse] = lambda: fake_clickhouse
    original_keystore = keystore_mod.keystore
    keystore_mod.keystore = FileKeyStore(base_dir=tmp_path / "keystore")
    keystore_mod.keystore.ensure_keypair()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        keystore_mod.keystore = original_keystore


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
    app.dependency_overrides[require_user] = lambda: MagicMock()
    app.dependency_overrides[get_session] = lambda: _FakeAuthSession(
        project, membership
    )


def test_llm_messages_read_rejects_anonymous(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    """No cookie / dev header → the require_org_member chain 401s before the
    handler runs, so ClickHouse is never queried."""
    r = client.get(_READ_URL)
    assert r.status_code == 401
    fake_clickhouse.query.assert_not_called()


def test_llm_messages_read_unknown_project_is_404(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    """Authenticated but the project doesn't exist → 404, so project IDs
    can't be enumerated via 403-vs-404 differences."""
    _login_as_stub_user(project=None, membership=None)
    r = client.get(_READ_URL)
    assert r.status_code == 404
    fake_clickhouse.query.assert_not_called()


def test_llm_messages_read_non_member_is_403(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    """The load-bearing tenant test: a user outside the project's org can't
    read its transcripts, even holding a valid session and the project id."""
    _login_as_stub_user(project=MagicMock(org_id="org_other"), membership=None)
    r = client.get(_READ_URL)
    assert r.status_code == 403
    fake_clickhouse.query.assert_not_called()


def test_llm_messages_read_member_is_200(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    """Authenticated + membership row present → the handler answers, and the
    project it queries is the path's, never a caller-supplied one."""
    _login_as_stub_user(project=MagicMock(org_id="org_1"), membership=MagicMock())
    r = client.get(_READ_URL)
    assert r.status_code == 200, r.text
    assert r.json() == {"rows": [], "total": 0, "limit": 50, "offset": 0}
    assert fake_clickhouse.query.call_args.kwargs["parameters"]["pid"] == "proj_test"


def test_llm_messages_read_returns_the_page(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    """A stored row round-trips through LlmMessageRow: content decoded, flags
    as booleans, an absent run_id as null."""
    app.dependency_overrides[require_org_member] = lambda: MagicMock()
    fake_clickhouse.query.return_value.result_rows = [
        _stored_row(truncated=1, total_matches=1)
    ]

    r = client.get(_READ_URL)

    assert r.status_code == 200, r.text
    (row,) = r.json()["rows"]
    assert row["input_messages"] == json.loads(_INPUT)
    assert row["truncated"] is True and row["resynced"] is False
    assert row["run_id"] is None
    assert row["message_seq"] == 0


@pytest.mark.parametrize(
    "query",
    [
        "",  # neither scope
        "?session_id=",  # blank session
        f"?run_id={ZERO_RUN_ID}",  # the "outside any run" value
    ],
)
def test_when_no_usable_scope_is_given_then_422(
    client: TestClient, fake_clickhouse: MagicMock, query: str
) -> None:
    """Some scope is not optional: without one the read would be a
    project-wide dump of every stored prompt."""
    app.dependency_overrides[require_org_member] = lambda: MagicMock()
    r = client.get(f"{_READ_PATH}{query}")
    assert r.status_code == 422
    fake_clickhouse.query.assert_not_called()


def test_when_only_a_run_id_is_given_then_the_endpoint_serves_it(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    """The path for SDK users who never set a session id — without it their
    transcripts would be stored and unreadable for the whole 180-day TTL."""
    app.dependency_overrides[require_org_member] = lambda: MagicMock()
    run_id = uuid.uuid4()

    r = client.get(f"{_READ_PATH}?run_id={run_id}")

    assert r.status_code == 200, r.text
    assert fake_clickhouse.query.call_args.kwargs["parameters"]["run_id"] == run_id


def test_when_the_run_id_is_malformed_then_422(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    app.dependency_overrides[require_org_member] = lambda: MagicMock()
    r = client.get(f"{_READ_PATH}?run_id=not-a-uuid")
    assert r.status_code == 422
    fake_clickhouse.query.assert_not_called()


def test_when_limit_is_over_the_cap_then_it_is_clamped(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    """Message rows carry up to ~272 KiB of content, so an unbounded limit
    would let one request ask for a multi-gigabyte response."""
    app.dependency_overrides[require_org_member] = lambda: MagicMock()

    r = client.get(f"{_READ_URL}&limit=100000")

    assert r.status_code == 200, r.text
    assert fake_clickhouse.query.call_args.kwargs["parameters"]["lim"] == MAX_PAGE_SIZE


def test_when_limit_and_offset_are_negative_then_they_are_floored(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    app.dependency_overrides[require_org_member] = lambda: MagicMock()

    r = client.get(f"{_READ_URL}&limit=-5&offset=-5")

    assert r.status_code == 200, r.text
    params = fake_clickhouse.query.call_args.kwargs["parameters"]
    assert params["lim"] == 1 and params["off"] == 0


def test_when_clickhouse_is_down_then_503(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    """A transcript read that can't reach storage is unavailable, not empty —
    an empty 200 would read as "this session had no LLM calls"."""
    app.dependency_overrides[require_org_member] = lambda: MagicMock()
    fake_clickhouse.query.side_effect = OperationalError("clickhouse down")

    r = client.get(_READ_URL)

    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Integration — requires `make clickhouse-up` first; opt-in via marker
# ---------------------------------------------------------------------------


def _event(**overrides) -> LlmMessageEvent:
    base = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": datetime.now(timezone.utc),
        "agent_name": "researcher",
        "model": "gpt-4o",
        "turn_key": "run_1:researcher",
        "message_seq": 0,
        "input_messages": _INPUT,
        "output_messages": _OUTPUT,
    }
    return LlmMessageEvent(**{**base, **overrides})


@pytest.mark.integration
def test_list_llm_messages_round_trip() -> None:
    """Write through the real batch insert, read back through the real SELECT.

    The mocked tests above assert on the SQL *text* — they never ask
    ClickHouse to run it, so a wrong column name or a parameter type the
    driver binds differently (``{run_id:UUID}``) passes every one of them and
    500s in production. This is the test that executes the statement.
    """
    from hexgate_api.core.clickhouse import get_clickhouse as real_get_clickhouse

    clickhouse_client = real_get_clickhouse()
    project_id = f"test_proj_{uuid.uuid4().hex[:8]}"
    run_id = uuid.uuid4()
    start = datetime.now(timezone.utc).replace(microsecond=0)

    # Two rows in one named session, plus one from a run whose caller never set
    # a session id — the common case, reachable only by run_id.
    events = [
        _event(session_id="sess_1", message_seq=0, occurred_at=start, run_id=run_id),
        _event(
            session_id="sess_1",
            message_seq=1,
            occurred_at=start + timedelta(seconds=1),
            run_id=run_id,
            resynced=True,
            truncated=True,
            system_instructions=json.dumps({"content": "be terse"}),
        ),
        _event(
            message_seq=0, occurred_at=start + timedelta(seconds=2), run_id=run_id
        ),  # session_id defaults to ""
    ]
    insert_llm_messages_batch(
        clickhouse_client,
        [
            BatchItem(event, project_id=project_id, agent_version_id="ver_int")
            for event in events
        ],
    )

    try:
        by_session = list_llm_messages(
            clickhouse_client, project_id=project_id, session_id="sess_1"
        )
        assert by_session["total"] == 2
        first, second = by_session["rows"]
        # Ascending, so the transcript reads forwards.
        assert [first["message_seq"], second["message_seq"]] == [0, 1]
        assert first["input_messages"] == json.loads(_INPUT)
        assert first["output_messages"] == json.loads(_OUTPUT)
        assert first["system_instructions"] is None  # "" -> None
        assert second["system_instructions"] == {"content": "be terse"}
        assert (first["resynced"], first["truncated"]) == (0, 0)
        assert (second["resynced"], second["truncated"]) == (1, 1)
        assert first["run_id"] == run_id
        assert first["agent_version_id"] == "ver_int"
        assert first["received_at"] is not None  # server-stamped column default

        # The run scope reaches all three, including the session-less row that
        # a session-only endpoint would have stranded for its whole TTL.
        by_run = list_llm_messages(
            clickhouse_client, project_id=project_id, run_id=run_id
        )
        assert by_run["total"] == 3
        assert [row["session_id"] for row in by_run["rows"]] == ["sess_1", "sess_1", ""]

        # Both scopes narrow to the intersection.
        both = list_llm_messages(
            clickhouse_client,
            project_id=project_id,
            session_id="sess_1",
            run_id=run_id,
        )
        assert both["total"] == 2

        # Paging: limit/offset walk the same order the full read returned.
        page = list_llm_messages(
            clickhouse_client, project_id=project_id, run_id=run_id, limit=1, offset=2
        )
        assert page["total"] == 3
        assert page["rows"][0]["event_id"] == by_run["rows"][2]["event_id"]

        # Past the end: no rows to carry count() OVER (), so the fallback runs.
        past_end = list_llm_messages(
            clickhouse_client, project_id=project_id, run_id=run_id, limit=1, offset=99
        )
        assert past_end["rows"] == [] and past_end["total"] == 3
    finally:
        clickhouse_client.command(
            "ALTER TABLE llm_message DELETE WHERE project_id = {pid:String}",
            parameters={"pid": project_id},
        )
