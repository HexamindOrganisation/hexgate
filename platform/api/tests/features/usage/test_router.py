"""HTTP contract of GET /v1/agents/{name}/usage and POST /v1/usage/users:read —
the server halves of the SDK's usage wire contracts — and of the
dashboard's GET /v1/projects/{id}/usage/top-users. Fixtures
mirror tests/features/bans/test_bans.py."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import DEFAULT_ORG_ID
from hexgate_api.core import keystore as keystore_mod
from hexgate_api.core.ids import new_id
from hexgate_api.deps.clickhouse import clickhouse_getter
from hexgate_api.deps.org import require_org_member
from hexgate_api.deps.tokens import require_project
from hexgate_api.features.usage import router as usage_router
from hexgate_api.features.usage.service import (
    _USAGE_READ_THREAD_PREFIX,
    UsageMemo,
    get_usage_executor,
    get_usage_memo,
    get_user_usage_executor,
    get_user_usage_memo,
)
from hexgate_api.features.usage.stats import get_agent_read_stats, get_user_read_stats
from hexgate_api.main import app
from hexgate_api.models import Agent, Project
from hexgate_api.schemas import USER_ID_MAX_LENGTH, AuditEnvelope
from hexgate_api.seeds.defaults import ensure_default_project

_PROJECT = "proj_usage"
_OTHER_PROJECT = "proj_other"
_AGENT = "billing"
_ONLY_ELSEWHERE = "elsewhere"
_AS_OF = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
_SHORT_TIMEOUT = 0.05
# Bounded, so a worker thread the timeout abandoned can't hang loop shutdown.
_STALL_SECONDS = 0.5
_TEST_POOL_PREFIX = "usage-test-pool"


async def _add_agent(factory, project_id: str, name: str) -> None:
    async with factory() as s:
        s.add(
            Agent(
                id=new_id(Agent),
                project_id=project_id,
                name=name,
                agent_yaml=f"name: {name}\n",
                policy_yaml="version: 1\ntools: {}\n",
            )
        )
        await s.commit()


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as bootstrap:
        await ensure_default_project(bootstrap)
        bootstrap.add(Project(id=_PROJECT, org_id=DEFAULT_ORG_ID, name="usage"))
        bootstrap.add(Project(id=_OTHER_PROJECT, org_id=DEFAULT_ORG_ID, name="other"))
        await bootstrap.commit()
    await _add_agent(factory, _PROJECT, _AGENT)
    await _add_agent(factory, _OTHER_PROJECT, _ONLY_ELSEWHERE)
    yield factory
    await engine.dispose()


@pytest.fixture
def fake_clickhouse() -> MagicMock:
    client = MagicMock()
    client.query.return_value.result_rows = [[_AS_OF, 5, 5]]
    return client


@pytest_asyncio.fixture
async def anonymous_client(session_factory, fake_clickhouse: MagicMock, tmp_path):
    """Everything stubbed but the bearer: auth runs for real."""
    from hexgate_api.core.db import get_session
    from hexgate_api.core.keystore import FileKeyStore

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[clickhouse_getter] = lambda: lambda: fake_clickhouse
    memo = UsageMemo(1.0, 100, lambda: 0.0)  # frozen clock: never expires
    user_memo = UsageMemo(1.0, 100, lambda: 0.0)
    app.dependency_overrides[get_usage_memo] = lambda: memo
    app.dependency_overrides[get_user_usage_memo] = lambda: user_memo
    original_keystore = keystore_mod.keystore
    keystore_mod.keystore = FileKeyStore(base_dir=tmp_path / "keystore")
    keystore_mod.keystore.ensure_keypair()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        keystore_mod.keystore = original_keystore


@pytest.fixture
def client(anonymous_client: TestClient) -> TestClient:
    app.dependency_overrides[require_project] = lambda: _PROJECT
    return anonymous_client


def _usage(client: TestClient, paths: str, agent: str = _AGENT):
    return client.get(f"/v1/agents/{agent}/usage", params={"paths": paths})


def test_values_are_keyed_by_exactly_the_requested_paths(client: TestClient) -> None:
    r = _usage(client, "invocations_1h,denials_5m")

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"as_of", "values"}
    assert body["values"] == {"invocations_1h": 5, "denials_5m": 5}
    assert datetime.fromisoformat(body["as_of"]) == _AS_OF
    assert r.headers["Cache-Control"] == "private, max-age=1"


def test_two_spellings_of_one_window_share_one_column(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    fake_clickhouse.query.return_value.result_rows = [[_AS_OF, 11]]

    r = _usage(client, "invocations_1h,invocations_60m")

    assert r.json()["values"] == {"invocations_1h": 11, "invocations_60m": 11}
    sql = fake_clickhouse.query.call_args.args[0]
    assert sql.count("sumIf(") == 1


@pytest.mark.parametrize("agent", ["unknown", _ONLY_ELSEWHERE])
def test_an_agent_not_in_the_bearer_project_is_404(
    client: TestClient, fake_clickhouse: MagicMock, agent: str
) -> None:
    r = _usage(client, "invocations_1h", agent=agent)

    assert r.status_code == 404
    assert r.json()["detail"] == "agent not found"
    fake_clickhouse.query.assert_not_called()


@pytest.mark.parametrize("params", [{"paths": "errors_1h"}, {"paths": ""}, {}])
def test_bad_or_missing_paths_are_422(
    client: TestClient, fake_clickhouse: MagicMock, params: dict[str, str]
) -> None:
    r = client.get(f"/v1/agents/{_AGENT}/usage", params=params)

    assert r.status_code == 422
    fake_clickhouse.query.assert_not_called()


def test_a_clickhouse_failure_is_503_and_not_memoized(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    fake_clickhouse.query.side_effect = DatabaseError("down")

    first = _usage(client, "invocations_1h")
    second = _usage(client, "invocations_1h")

    assert first.status_code == second.status_code == 503
    assert first.headers["Retry-After"] == "5"
    assert first.json()["detail"] == "usage temporarily unavailable"
    assert fake_clickhouse.query.call_count == 2


def test_a_read_slower_than_the_timeout_is_503_and_not_memoized(
    client: TestClient, fake_clickhouse: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(usage_router, "USAGE_READ_TIMEOUT_SECONDS", _SHORT_TIMEOUT)
    released = threading.Event()

    def stalled_query(*_args: object, **_kwargs: object) -> None:
        released.wait(_STALL_SECONDS)

    fake_clickhouse.query.side_effect = stalled_query
    try:
        first = _usage(client, "invocations_1h")
        second = _usage(client, "invocations_1h")
    finally:
        released.set()

    assert first.status_code == second.status_code == 503
    assert first.headers["Retry-After"] == "5"
    assert first.json()["detail"] == "usage temporarily unavailable"
    assert fake_clickhouse.query.call_count == 2


@pytest.fixture
def unreachable_clickhouse() -> MagicMock:
    connect = MagicMock(side_effect=OperationalError("connection refused"))
    app.dependency_overrides[clickhouse_getter] = lambda: connect
    return connect


def test_an_unreachable_clickhouse_is_503_and_retried(
    client: TestClient, unreachable_clickhouse: MagicMock
) -> None:
    first = _usage(client, "invocations_1h")
    second = _usage(client, "invocations_1h")

    assert first.status_code == second.status_code == 503
    assert first.json()["detail"] == "usage temporarily unavailable"
    assert unreachable_clickhouse.call_count == 2


@pytest.mark.parametrize(
    ("paths", "agent", "expected"),
    [("errors_1h", _AGENT, 422), ("invocations_1h", "unknown", 404)],
)
def test_validation_runs_before_any_connect(
    client: TestClient,
    unreachable_clickhouse: MagicMock,
    paths: str,
    agent: str,
    expected: int,
) -> None:
    assert _usage(client, paths, agent=agent).status_code == expected
    unreachable_clickhouse.assert_not_called()


def test_the_read_runs_on_the_injected_pool(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    threads: list[str] = []
    result = fake_clickhouse.query.return_value
    result.result_rows = [[_AS_OF, 1]]

    def recording_query(*_args: object, **_kwargs: object) -> MagicMock:
        threads.append(threading.current_thread().name)
        return result

    fake_clickhouse.query.side_effect = recording_query
    with ThreadPoolExecutor(1, thread_name_prefix=_TEST_POOL_PREFIX) as pool:
        app.dependency_overrides[get_usage_executor] = lambda: pool
        assert _usage(client, "invocations_1h").status_code == 200

    assert threads[0].startswith(_TEST_POOL_PREFIX)


def test_the_postgres_transaction_ends_before_the_clickhouse_read(
    client: TestClient, session_factory, fake_clickhouse: MagicMock
) -> None:
    from hexgate_api.core.db import get_session

    sessions: list[AsyncSession] = []

    async def capturing_session():
        async with session_factory() as session:
            sessions.append(session)
            yield session

    app.dependency_overrides[get_session] = capturing_session
    in_transaction: list[bool] = []
    result = fake_clickhouse.query.return_value

    def recording_query(*_args: object, **_kwargs: object) -> MagicMock:
        in_transaction.append(sessions[0].in_transaction())
        return result

    fake_clickhouse.query.side_effect = recording_query

    assert _usage(client, "invocations_1h,denials_5m").status_code == 200
    assert in_transaction == [False]


def test_no_bearer_is_401(anonymous_client: TestClient) -> None:
    assert _usage(anonymous_client, "invocations_1h").status_code == 401


def test_two_requests_inside_the_ttl_query_once(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    fake_clickhouse.query.return_value.result_rows = [[_AS_OF, 1]]

    _usage(client, "invocations_1h")
    r = _usage(client, "invocations_60m")

    assert r.json()["values"] == {"invocations_60m": 1}
    assert fake_clickhouse.query.call_count == 1


# ---------------------------------------------------------------------------
# POST /v1/usage/users:read
# ---------------------------------------------------------------------------

_USER = "alice@example.com"
_USER_PATH = "/v1/usage/users:read"
_USER_POOL_PREFIX = "usage-test-user-pool"
_RELEASE_SECONDS = 2.0
_MULTIBYTE = "é"  # 2 bytes in UTF-8
# Two specs, matching fake_clickhouse's default row of two values.
_TWO_PATHS = ["invocations_1h", "denials_5m"]


def _user_usage(client: TestClient, paths: list[str], user_id: str = _USER):
    return client.post(_USER_PATH, json={"user_id": user_id, "paths": paths})


def test_user_values_are_keyed_by_exactly_the_requested_paths(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    r = _user_usage(client, ["invocations_1h", "denials_5m"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"as_of", "values"}
    assert body["values"] == {"invocations_1h": 5, "denials_5m": 5}
    assert datetime.fromisoformat(body["as_of"]) == _AS_OF
    assert r.headers["Cache-Control"] == "private, no-store"
    sql = fake_clickhouse.query.call_args.args[0]
    params = fake_clickhouse.query.call_args.kwargs["parameters"]
    assert "FROM usage_minute_user\n" in sql
    assert params["user_id"] == _USER
    assert params["project_id"] == _PROJECT


def test_an_unknown_user_reads_zeros_not_404(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    fake_clickhouse.query.return_value.result_rows = [[_AS_OF, 0]]

    r = _user_usage(client, ["invocations_1h"], user_id="nobody")

    assert r.status_code == 200
    assert r.json()["values"] == {"invocations_1h": 0}


@pytest.mark.parametrize(
    ("user_id", "detail"),
    [
        ("", "user_id is empty"),
        ("a" * 257, "user_id is longer than 256 characters"),
        (_MULTIBYTE * 257, "user_id is longer than 256 characters"),
    ],
    ids=["empty", "257-ascii", "257-multibyte"],
)
def test_a_bad_user_id_is_422_without_echoing_it(
    client: TestClient, fake_clickhouse: MagicMock, user_id: str, detail: str
) -> None:
    r = _user_usage(client, ["invocations_1h"], user_id=user_id)

    assert r.status_code == 422
    assert r.json()["detail"] == detail
    if user_id:
        assert user_id not in r.text
    fake_clickhouse.query.assert_not_called()


def test_every_user_id_ingest_accepts_can_be_read(client: TestClient) -> None:
    """The cap counts characters, as AuditEnvelope.user_id does: a 256-character
    multibyte id (512 bytes) is counted by the rollup, so it must be readable."""
    user_id = _MULTIBYTE * USER_ID_MAX_LENGTH
    AuditEnvelope(event_id=uuid4(), occurred_at=_AS_OF, agent_name="a", user_id=user_id)

    assert _user_usage(client, _TWO_PATHS, user_id=user_id).status_code == 200


def test_a_lone_surrogate_user_id_is_422_without_echoing_it(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    # Raw bytes: httpx can't encode a lone surrogate through json=.
    r = client.post(
        _USER_PATH,
        content=b'{"user_id": "\\ud800", "paths": ["invocations_1h"]}',
        headers={"content-type": "application/json"},
    )

    assert r.status_code == 422
    assert r.json()["detail"] == "user_id is not valid UTF-8"
    assert "ud800" not in r.text
    fake_clickhouse.query.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"user_id": _USER, "paths": ["errors_1h"]},
        {"user_id": _USER, "paths": []},
        {"user_id": _USER, "paths": ["invocations_1h"] * 65},
        {"paths": ["invocations_1h"]},
        {"user_id": _USER},
        {"user_id": _USER, "paths": "invocations_1h"},
        {"user_id": _USER, "paths": ["invocations_1h"], "verified": True},
        {"user_id": _USER, "paths": ["invocations_1h"], "project_id": _OTHER_PROJECT},
    ],
    ids=[
        "bad-path",
        "no-paths",
        "65-paths",
        "missing-user",
        "missing-paths",
        "paths-not-a-list",
        "extra-field",
        "project-in-body",
    ],
)
def test_a_bad_body_is_422_before_any_query(
    client: TestClient, fake_clickhouse: MagicMock, body: dict[str, object]
) -> None:
    assert client.post(_USER_PATH, json=body).status_code == 422
    fake_clickhouse.query.assert_not_called()


def test_a_user_read_failure_is_503_and_never_logs_the_user_id(
    client: TestClient, fake_clickhouse: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    fake_clickhouse.query.side_effect = DatabaseError(f"bad value {_USER!r} in query")

    with caplog.at_level("DEBUG"):
        first = _user_usage(client, ["invocations_1h"])
        second = _user_usage(client, ["invocations_1h"])

    assert first.status_code == second.status_code == 503
    assert first.headers["Retry-After"] == "5"
    assert first.json()["detail"] == "usage temporarily unavailable"
    assert fake_clickhouse.query.call_count == 2
    assert "usage read failed for a user in project" in caplog.text
    assert _USER not in caplog.text


def test_a_user_read_slower_than_the_timeout_is_503_and_not_memoized(
    client: TestClient, fake_clickhouse: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(usage_router, "USAGE_READ_TIMEOUT_SECONDS", _SHORT_TIMEOUT)
    released = threading.Event()

    def stalled_query(*_args: object, **_kwargs: object) -> None:
        released.wait(_STALL_SECONDS)

    fake_clickhouse.query.side_effect = stalled_query
    try:
        first = _user_usage(client, ["invocations_1h"])
        second = _user_usage(client, ["invocations_1h"])
    finally:
        released.set()

    assert first.status_code == second.status_code == 503
    assert fake_clickhouse.query.call_count == 2


def test_the_agent_failure_log_names_the_class_and_code_not_the_message(
    client: TestClient, fake_clickhouse: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    fake_clickhouse.query.side_effect = DatabaseError(
        "Received ClickHouse exception, code: 241, server response: secret detail"
    )

    with caplog.at_level("WARNING"):
        assert _usage(client, "invocations_1h").status_code == 503

    assert "DatabaseError (clickhouse code 241)" in caplog.text
    assert "secret detail" not in caplog.text


def test_the_user_failure_log_carries_the_code_but_not_the_user_id(
    client: TestClient, fake_clickhouse: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    fake_clickhouse.query.side_effect = DatabaseError(
        f"Received ClickHouse exception, code: 60, server response: bad value {_USER!r}"
    )

    with caplog.at_level("WARNING"):
        assert _user_usage(client, ["invocations_1h"]).status_code == 503

    assert "clickhouse code 60" in caplog.text
    assert _USER not in caplog.text


def test_a_failure_without_a_server_code_logs_none(
    client: TestClient, fake_clickhouse: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    fake_clickhouse.query.side_effect = DatabaseError("connection reset")

    with caplog.at_level("WARNING"):
        assert _usage(client, "invocations_1h").status_code == 503

    assert "DatabaseError (clickhouse code None)" in caplog.text


def test_same_user_same_window_queries_once_and_two_users_twice(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    fake_clickhouse.query.return_value.result_rows = [[_AS_OF, 1]]

    _user_usage(client, ["invocations_1h"])
    _user_usage(client, ["invocations_60m"])
    assert fake_clickhouse.query.call_count == 1

    _user_usage(client, ["invocations_1h"], user_id="bob")
    assert fake_clickhouse.query.call_count == 2


def test_an_agent_and_a_user_sharing_a_name_never_share_a_memo_entry(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    fake_clickhouse.query.return_value.result_rows = [[_AS_OF, 1]]

    _usage(client, "invocations_1h", agent=_AGENT)
    _user_usage(client, ["invocations_1h"], user_id=_AGENT)

    assert fake_clickhouse.query.call_count == 2


def test_the_agent_and_user_scopes_share_no_memo_pool_or_stats() -> None:
    agent = usage_router.get_agent_scope(
        get_usage_memo(), get_usage_executor(), get_agent_read_stats()
    )
    user = usage_router.get_user_scope(
        get_user_usage_memo(), get_user_usage_executor(), get_user_read_stats()
    )

    assert agent.rollup is not user.rollup
    assert agent.memo is not user.memo
    assert agent.executor is not user.executor
    assert agent.stats is not user.stats


def test_the_user_read_runs_on_the_user_pool(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    threads: list[str] = []
    result = fake_clickhouse.query.return_value
    result.result_rows = [[_AS_OF, 1]]

    def recording_query(*_args: object, **_kwargs: object) -> MagicMock:
        threads.append(threading.current_thread().name)
        return result

    fake_clickhouse.query.side_effect = recording_query
    with ThreadPoolExecutor(1, thread_name_prefix=_USER_POOL_PREFIX) as pool:
        app.dependency_overrides[get_user_usage_executor] = lambda: pool
        assert _user_usage(client, ["invocations_1h"]).status_code == 200

    assert threads[0].startswith(_USER_POOL_PREFIX)


def test_a_blocked_user_pool_never_delays_an_agent_read(
    client: TestClient, fake_clickhouse: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A burst of cold users must not queue agent reads behind it."""
    monkeypatch.setattr(usage_router, "USAGE_READ_TIMEOUT_SECONDS", _SHORT_TIMEOUT)
    released = threading.Event()
    result = fake_clickhouse.query.return_value
    result.result_rows = [[_AS_OF, 1]]

    def query(sql: str, *_args: object, **_kwargs: object) -> MagicMock:
        if "usage_minute_user" in sql:
            released.wait(_RELEASE_SECONDS)
        return result

    fake_clickhouse.query.side_effect = query
    with (
        ThreadPoolExecutor(1, thread_name_prefix=_USER_POOL_PREFIX) as user_pool,
        ThreadPoolExecutor(1, thread_name_prefix=_TEST_POOL_PREFIX) as agent_pool,
    ):
        app.dependency_overrides[get_user_usage_executor] = lambda: user_pool
        app.dependency_overrides[get_usage_executor] = lambda: agent_pool
        try:
            user = _user_usage(client, ["invocations_1h"])
            agent = _usage(client, "invocations_1h")
        finally:
            released.set()

    assert user.status_code == 503
    assert agent.status_code == 200


def test_the_user_route_never_opens_a_postgres_session(client: TestClient) -> None:
    from hexgate_api.core.db import get_session

    async def failing_session():
        raise AssertionError("the user route opened a Postgres session")
        yield

    app.dependency_overrides[get_session] = failing_session

    assert _user_usage(client, _TWO_PATHS).status_code == 200


def test_no_bearer_on_the_user_route_is_401(anonymous_client: TestClient) -> None:
    assert _user_usage(anonymous_client, ["invocations_1h"]).status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/usage/users"),
        ("post", "/v1/usage/users:write"),
        ("get", _USER_PATH),
    ],
)
def test_only_the_literal_read_path_routes(
    client: TestClient, method: str, path: str
) -> None:
    r = client.request(
        method.upper(), path, json={"user_id": _USER, "paths": ["invocations_1h"]}
    )

    assert r.status_code in {404, 405}


class _RecordingStats:
    def __init__(self) -> None:
        self.records: list[tuple[bool, bool]] = []

    def record(self, *, latency_seconds: float, queried: bool, failed: bool) -> None:
        assert latency_seconds >= 0
        self.records.append((queried, failed))


def test_each_route_records_into_its_own_scope(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    fake_clickhouse.query.return_value.result_rows = [[_AS_OF, 1]]
    agent_stats, user_stats = _RecordingStats(), _RecordingStats()
    app.dependency_overrides[get_agent_read_stats] = lambda: agent_stats
    app.dependency_overrides[get_user_read_stats] = lambda: user_stats

    _user_usage(client, ["invocations_1h"])
    _user_usage(client, ["invocations_1h"])
    _usage(client, "invocations_1h")

    assert user_stats.records == [(True, False), (False, False)]
    assert agent_stats.records == [(True, False)]


def test_a_failed_read_is_recorded_as_a_failure(
    client: TestClient, fake_clickhouse: MagicMock
) -> None:
    fake_clickhouse.query.side_effect = DatabaseError("down")
    user_stats = _RecordingStats()
    app.dependency_overrides[get_user_read_stats] = lambda: user_stats

    _user_usage(client, ["invocations_1h"])

    assert user_stats.records == [(True, True)]


# ---------------------------------------------------------------------------
# GET /v1/projects/{project_id}/usage/top-users: the dashboard ranking
# ---------------------------------------------------------------------------

_TOP_USERS_PATH = f"/v1/projects/{_PROJECT}/usage/top-users"
_BLOCKED_POOL_PREFIX = "usage-test-blocked-user-pool"


@pytest.fixture
def dashboard(anonymous_client: TestClient, fake_clickhouse: MagicMock) -> TestClient:
    app.dependency_overrides[require_org_member] = lambda: MagicMock()
    fake_clickhouse.query.return_value.result_rows = [(_USER, 50), ("bob", 30)]
    return anonymous_client


def test_top_users_without_a_session_is_401(
    anonymous_client: TestClient, fake_clickhouse: MagicMock
) -> None:
    r = anonymous_client.get(_TOP_USERS_PATH)

    assert r.status_code == 401
    fake_clickhouse.query.assert_not_called()


def test_top_users_answers_the_ranking_and_is_never_stored(
    dashboard: TestClient,
) -> None:
    r = dashboard.get(_TOP_USERS_PATH, params={"window": "7d", "metric": "denials"})

    assert r.status_code == 200, r.text
    assert r.json() == {
        "window": "7d",
        "metric": "denials",
        "users": [{"user_id": _USER, "value": 50}, {"user_id": "bob", "value": 30}],
    }
    assert r.headers["Cache-Control"] == "private, no-store"


def test_top_users_defaults_reach_the_query(
    dashboard: TestClient, fake_clickhouse: MagicMock
) -> None:
    r = dashboard.get(_TOP_USERS_PATH)

    assert r.status_code == 200, r.text
    assert (r.json()["window"], r.json()["metric"]) == ("24h", "total_tokens")
    sql = fake_clickhouse.query.call_args.args[0]
    params = fake_clickhouse.query.call_args.kwargs["parameters"]
    assert "sum(input_tokens + output_tokens)" in sql
    assert params == {"project_id": _PROJECT, "window": 86_400, "limit": 10}


@pytest.mark.parametrize(
    "params",
    [{"window": "90d"}, {"metric": "cost_usd"}, {"limit": 0}, {"limit": 51}],
)
def test_top_users_rejects_a_bad_query_with_422(
    dashboard: TestClient, fake_clickhouse: MagicMock, params: dict[str, object]
) -> None:
    r = dashboard.get(_TOP_USERS_PATH, params=params)

    assert r.status_code == 422
    fake_clickhouse.query.assert_not_called()


def test_a_failed_top_users_read_is_503_and_logs_no_message(
    dashboard: TestClient,
    fake_clickhouse: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ClickHouse message can quote a bound value."""
    quoted = "value quoted by clickhouse"
    fake_clickhouse.query.side_effect = DatabaseError(quoted)

    with caplog.at_level("WARNING", logger=usage_router.__name__):
        r = dashboard.get(_TOP_USERS_PATH)

    assert r.status_code == 503
    assert r.headers["Retry-After"] == "5"
    assert r.json()["detail"] == "usage temporarily unavailable"
    assert "DatabaseError" in caplog.text
    assert quoted not in caplog.text


def test_an_unreachable_clickhouse_on_top_users_is_the_same_usage_503(
    dashboard: TestClient,
    unreachable_clickhouse: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A connect failure answers and logs like a failed read, not as the audit
    log's (require_clickhouse), whose log line carries the message."""
    with caplog.at_level("WARNING"):
        r = dashboard.get(_TOP_USERS_PATH)

    assert r.status_code == 503
    assert r.json()["detail"] == "usage temporarily unavailable"
    assert unreachable_clickhouse.call_count == 1
    assert "OperationalError" in caplog.text
    assert "connection refused" not in caplog.text


def test_top_users_never_touches_the_sdk_user_pool_or_stats(
    dashboard: TestClient, fake_clickhouse: MagicMock
) -> None:
    """Dashboard tabs must not queue SDK user reads or move their read rate."""
    threads: list[str] = []
    result = fake_clickhouse.query.return_value

    def recording_query(*_args: object, **_kwargs: object) -> MagicMock:
        threads.append(threading.current_thread().name)
        return result

    fake_clickhouse.query.side_effect = recording_query
    released = threading.Event()
    stats = MagicMock()
    with ThreadPoolExecutor(1, thread_name_prefix=_BLOCKED_POOL_PREFIX) as pool:
        pool.submit(released.wait, _RELEASE_SECONDS)
        app.dependency_overrides[get_user_usage_executor] = lambda: pool
        app.dependency_overrides[get_user_read_stats] = lambda: stats
        try:
            r = dashboard.get(_TOP_USERS_PATH)
        finally:
            released.set()

    assert r.status_code == 200, r.text
    assert not threads[0].startswith(_BLOCKED_POOL_PREFIX)
    # Also off the real pools, so calling the executor directly instead of through
    # the overridden dependency still fails ("usage-read-user" shares the prefix).
    assert not threads[0].startswith(_USAGE_READ_THREAD_PREFIX)
    stats.record.assert_not_called()
