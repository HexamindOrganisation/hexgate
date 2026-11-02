"""The background purge of dead OAuth rows, and the task that runs it."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, update
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api import main
from hexgate_api.features.oauth import cleanup, grants
from hexgate_api.models import OAuthAuthorizationCode, OAuthRefreshToken
from tests.features.oauth.consent_flow import login, redeem, refresh, rows, set_all

DAY = timedelta(days=1)
# A rotation copies created_at; last_used_at grows along a chain (None first).
_OLDEST_FIRST = OAuthRefreshToken.last_used_at.nulls_first()


def _ago(age: timedelta) -> datetime:
    return datetime.now(timezone.utc) - age


async def _purge(engine) -> None:
    async with AsyncSession(engine) as session:
        await grants.purge_dead_rows(session)


async def _set(engine, row_id: str, **values) -> None:
    async with AsyncSession(engine) as session:
        await session.exec(
            update(OAuthRefreshToken)
            .where(OAuthRefreshToken.id == row_id)
            .values(**values)
        )
        await session.commit()


async def _chain(client: TestClient, engine, length: int) -> list[OAuthRefreshToken]:
    """A login refreshed ``length - 1`` times: its rows, oldest first."""
    token = login(client)["refresh_token"]
    for _ in range(length - 1):
        token = refresh(client, token).json()["refresh_token"]
    return await rows(engine, OAuthRefreshToken, _OLDEST_FIRST)


@pytest.mark.parametrize(
    ("ended", "kept"),
    [
        pytest.param(6 * DAY, True, id="dead-6-days"),
        pytest.param(8 * DAY, False, id="dead-8-days"),
    ],
)
async def test_when_login_dead_past_retention_then_purged_whole(
    consent_client: TestClient, engine, ended: timedelta, kept: bool
) -> None:
    await _chain(consent_client, engine, 3)
    await set_all(engine, OAuthRefreshToken, revoked_at=_ago(ended))
    await _purge(engine)
    assert len(await rows(engine, OAuthRefreshToken)) == (3 if kept else 0)


async def test_when_login_expired_past_retention_then_purged(
    consent_client: TestClient, engine
) -> None:
    login(consent_client)
    await set_all(engine, OAuthRefreshToken, expires_at=_ago(8 * DAY))
    await _purge(engine)
    assert await rows(engine, OAuthRefreshToken) == []


async def test_when_login_lives_then_its_rotated_rows_survive(
    consent_client: TestClient, engine
) -> None:
    old, _ = await _chain(consent_client, engine, 2)
    # Replaced and expired long ago, but its login still has a live token.
    await _set(engine, old.id, revoked_at=_ago(60 * DAY), expires_at=_ago(40 * DAY))
    await _purge(engine)
    assert len(await rows(engine, OAuthRefreshToken)) == 2


async def test_when_thief_kept_login_alive_then_month_late_reuse_still_revokes(
    consent_client: TestClient, engine
) -> None:
    users = login(consent_client)["refresh_token"]
    thief = refresh(consent_client, users).json()["refresh_token"]
    old, _ = await rows(engine, OAuthRefreshToken, _OLDEST_FIRST)
    # The thief has refreshed for 38 days; the user's client was idle all along.
    await _set(engine, old.id, revoked_at=_ago(38 * DAY), expires_at=_ago(8 * DAY))
    await _purge(engine)
    assert refresh(consent_client, users).json()["error"] == "invalid_grant"
    assert refresh(consent_client, thief).json()["error"] == "invalid_grant"


async def test_when_purge_batch_cuts_a_chain_then_no_fk_error(
    consent_client: TestClient, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Foreign keys enforced, as on Postgres: SQLite ignores them by default
    # (one connection: the engine's pool is static).
    async with engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    chain = await _chain(consent_client, engine, 3)
    # Rewritten newest-first, so a batch in table order takes a row its
    # predecessor's replaced_by_id still names.
    copies = [OAuthRefreshToken(**row.model_dump()) for row in reversed(chain)]
    async with AsyncSession(engine) as session:
        await session.exec(update(OAuthRefreshToken).values(replaced_by_id=None))
        await session.exec(delete(OAuthRefreshToken))
        session.add_all(copies)
        await session.commit()
    await set_all(engine, OAuthRefreshToken, revoked_at=_ago(8 * DAY))
    monkeypatch.setattr(grants, "PURGE_BATCH", 1)
    for left in (2, 1, 0):
        await _purge(engine)
        assert len(await rows(engine, OAuthRefreshToken)) == left


@pytest.mark.parametrize(
    ("expired", "kept"),
    [
        pytest.param(timedelta(minutes=59), True, id="59-minutes"),
        pytest.param(timedelta(minutes=61), False, id="61-minutes"),
    ],
)
async def test_when_code_never_redeemed_then_purged_an_hour_after_expiry(
    consent_client: TestClient, engine, expired: timedelta, kept: bool
) -> None:
    from tests.features.oauth.consent_flow import code_of, consent

    code_of(consent(consent_client))
    await set_all(engine, OAuthAuthorizationCode, expires_at=_ago(expired))
    await _purge(engine)
    assert len(await rows(engine, OAuthAuthorizationCode)) == (1 if kept else 0)


async def test_when_code_issued_a_login_then_kept_until_the_login_goes(
    consent_client: TestClient, engine
) -> None:
    login(consent_client)
    await set_all(engine, OAuthAuthorizationCode, expires_at=_ago(2 * DAY))
    await _purge(engine)
    assert (
        len(await rows(engine, OAuthAuthorizationCode)) == 1
    )  # a replay still revokes
    await set_all(engine, OAuthRefreshToken, revoked_at=_ago(8 * DAY))
    await _purge(engine)  # the login goes, and with it the code
    assert await rows(engine, OAuthRefreshToken) == []
    assert await rows(engine, OAuthAuthorizationCode) == []


def test_when_exchanging_then_no_purge_on_the_token_path(
    consent_client: TestClient, engine
) -> None:
    deletes = []

    def record(conn, cursor, statement, *args) -> None:
        if statement.startswith("DELETE"):
            deletes.append(statement)

    event.listen(engine.sync_engine, "after_cursor_execute", record)
    try:
        token = login(consent_client)["refresh_token"]
        refresh(consent_client, token)
        redeem(consent_client, "not-a-code")
    finally:
        event.remove(engine.sync_engine, "after_cursor_execute", record)
    assert deletes == []


async def test_when_purge_raises_then_logged_and_next_run_still_happens(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runs = []
    done = asyncio.Event()

    async def flaky(session):
        runs.append(session)
        if len(runs) == 1:
            raise RuntimeError("deadlock detected")
        done.set()

    monkeypatch.setattr(grants, "purge_dead_rows", flaky)
    task = asyncio.create_task(cleanup.purge_periodically(first_delay=0, interval=0))
    try:
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        task.cancel()
    assert len(runs) >= 2
    (record,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert record.getMessage() == "oauth purge failed; retrying next run"


async def test_when_purging_block_runs_then_task_started_and_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = []

    async def fake_loop():
        states.append("started")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            states.append("stopped")
            raise

    monkeypatch.setattr(cleanup, "purge_periodically", fake_loop)
    async with cleanup.purging():
        await asyncio.sleep(0)
        assert states == ["started"]
    assert states == ["started", "stopped"]


def test_when_app_starts_then_its_lifespan_runs_the_purge() -> None:
    # The lifespan itself needs the real database and ClickHouse; its wiring
    # is the one line that wraps the app's lifetime in the purging block.
    assert "async with oauth_cleanup.purging():" in inspect.getsource(main.lifespan)
