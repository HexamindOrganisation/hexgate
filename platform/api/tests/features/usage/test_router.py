"""HTTP contract of GET /v1/agents/{name}/usage — the server half of PR 6's wire
contract. Fixtures mirror tests/features/bans/test_bans.py."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from clickhouse_connect.driver.exceptions import DatabaseError
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import DEFAULT_ORG_ID
from hexgate_api.core import keystore as keystore_mod
from hexgate_api.core.ids import new_id
from hexgate_api.deps.clickhouse import require_clickhouse
from hexgate_api.deps.tokens import require_project
from hexgate_api.features.usage import router as usage_router
from hexgate_api.features.usage.service import UsageMemo, get_usage_memo
from hexgate_api.main import app
from hexgate_api.models import Agent, Project
from hexgate_api.seeds.defaults import ensure_default_project

_PROJECT = "proj_usage"
_OTHER_PROJECT = "proj_other"
_AGENT = "billing"
_ONLY_ELSEWHERE = "elsewhere"
_AS_OF = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
_SHORT_TIMEOUT = 0.05
# Bounded, so a worker thread the timeout abandoned can't hang loop shutdown.
_STALL_SECONDS = 0.5


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
    app.dependency_overrides[require_clickhouse] = lambda: fake_clickhouse
    memo = UsageMemo(1.0, 100, lambda: 0.0)  # frozen clock: never expires
    app.dependency_overrides[get_usage_memo] = lambda: memo
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
    assert fake_clickhouse.query.call_count == 2


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
