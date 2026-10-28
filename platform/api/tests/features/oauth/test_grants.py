"""The user's live logins at ``/v1/oauth/grants``, and the bounded clean-up
each token exchange runs."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import DEFAULT_PROJECT_ID
from hexgate_api.features.oauth import grants, provider
from hexgate_api.models import OAuthAuthorizationCode, OAuthRefreshToken, utcnow
from tests.features.oauth.consent_flow import (
    CLIENT_ID,
    OTHER_USER_ID,
    USER_HEADERS,
    access_token,
    login,
    refresh,
    rows,
    set_all,
)

OTHER_USER_HEADERS = {"X-Dev-User": OTHER_USER_ID}


def _grants(client: TestClient, headers=USER_HEADERS):
    return client.get("/v1/oauth/grants", headers=headers)


def _revoke_grant(client: TestClient, grant_id: str, headers=USER_HEADERS):
    return client.delete(f"/v1/oauth/grants/{grant_id}", headers=headers)


async def test_list_grants_happy_path(consent_client: TestClient, engine) -> None:
    first = login(consent_client)
    refresh(consent_client, first["refresh_token"])
    resp = _grants(consent_client)
    assert resp.status_code == 200
    (grant,) = resp.json()  # one entry per login, not per rotation
    old, _ = await rows(engine, OAuthRefreshToken)
    assert grant["id"] == old.family_id
    assert grant["client_name"] == "Claude Code"
    assert grant["client_id"] == CLIENT_ID
    assert [s["name"] for s in grant["scopes"]] == ["policy:read", "audit:read"]
    assert all(s["description"] for s in grant["scopes"])
    (project,) = grant["projects"]
    assert project["id"] == DEFAULT_PROJECT_ID
    assert project["name"] and project["org_name"]
    # Aware, so the page reads it as UTC.
    assert grant["created_at"].endswith(("Z", "+00:00"))
    assert grant["last_used_at"] is not None


async def test_when_grant_not_live_then_not_listed(
    consent_client: TestClient, engine
) -> None:
    login(consent_client)
    await set_all(engine, OAuthRefreshToken, expires_at=utcnow() - timedelta(seconds=1))
    assert _grants(consent_client).json() == []
    await set_all(engine, OAuthRefreshToken, expires_at=utcnow() + timedelta(days=1))
    await set_all(engine, OAuthRefreshToken, revoked_at=utcnow())
    assert _grants(consent_client).json() == []


async def test_when_grants_listed_then_newest_first_and_never_another_users(
    consent_client: TestClient, engine
) -> None:
    login(consent_client)
    login(consent_client)
    older, newer = await rows(engine, OAuthRefreshToken, OAuthRefreshToken.created_at)
    listed = _grants(consent_client).json()
    assert [g["id"] for g in listed] == [newer.family_id, older.family_id]
    assert listed[0]["last_used_at"] is None  # never refreshed
    assert _grants(consent_client, OTHER_USER_HEADERS).json() == []


async def test_when_project_deleted_then_left_out_of_grant(
    consent_client: TestClient, engine
) -> None:
    login(consent_client)
    await set_all(engine, OAuthRefreshToken, projects_csv=f"{DEFAULT_PROJECT_ID},gone")
    (grant,) = _grants(consent_client).json()
    assert [p["id"] for p in grant["projects"]] == [DEFAULT_PROJECT_ID]


async def test_revoke_grant_happy_path(consent_client: TestClient, engine) -> None:
    token = login(consent_client)["refresh_token"]
    (grant,) = _grants(consent_client).json()
    assert _revoke_grant(consent_client, grant["id"]).status_code == 204
    assert _grants(consent_client).json() == []
    assert refresh(consent_client, token).json()["error"] == "invalid_grant"
    (row,) = await rows(engine, OAuthRefreshToken)
    assert row.revoked_by_user_id is not None


def test_when_another_users_grant_revoked_then_404_and_still_live(
    consent_client: TestClient,
) -> None:
    token = login(consent_client)["refresh_token"]
    (grant,) = _grants(consent_client).json()
    resp = _revoke_grant(consent_client, grant["id"], OTHER_USER_HEADERS)
    assert resp.status_code == 404
    assert refresh(consent_client, token).status_code == 200


def test_when_grant_unknown_or_already_revoked_then_404(
    consent_client: TestClient,
) -> None:
    login(consent_client)
    (grant,) = _grants(consent_client).json()
    assert _revoke_grant(consent_client, "not-a-grant").status_code == 404
    _revoke_grant(consent_client, grant["id"])
    assert _revoke_grant(consent_client, grant["id"]).status_code == 404


def test_when_grants_called_with_oauth_token_then_403(
    consent_client: TestClient,
) -> None:
    headers = {"Authorization": f"Bearer {access_token()}"}
    assert _grants(consent_client, headers).status_code == 403
    assert _revoke_grant(consent_client, "x", headers).status_code == 403


@pytest.mark.parametrize(
    ("model", "column", "age", "kept"),
    [
        (OAuthAuthorizationCode, "used_at", timedelta(hours=1, seconds=1), 0),
        (OAuthAuthorizationCode, "used_at", timedelta(minutes=59), 1),
        (OAuthAuthorizationCode, "expires_at", timedelta(hours=1, seconds=1), 0),
        (OAuthAuthorizationCode, "expires_at", timedelta(minutes=59), 1),
        (OAuthRefreshToken, "revoked_at", timedelta(days=7, seconds=1), 0),
        (OAuthRefreshToken, "revoked_at", timedelta(days=6), 1),
        (OAuthRefreshToken, "expires_at", timedelta(days=7, seconds=1), 0),
        (OAuthRefreshToken, "expires_at", timedelta(days=6), 1),
    ],
)
async def test_when_row_past_retention_then_purged_at_next_exchange(
    consent_client: TestClient, engine, model, column: str, age: timedelta, kept: int
) -> None:
    login(consent_client)
    await set_all(engine, model, **{column: utcnow() - age})
    login(consent_client)  # the purge runs before this login's rows are stale
    assert len(await rows(engine, model)) == kept + 1


async def test_when_refresh_exchanged_then_stale_rows_purged(
    consent_client: TestClient, engine
) -> None:
    token = login(consent_client)["refresh_token"]
    stale = utcnow() - timedelta(days=8)
    await set_all(engine, OAuthAuthorizationCode, used_at=stale)
    assert refresh(consent_client, token).status_code == 200
    assert await rows(engine, OAuthAuthorizationCode) == []


async def test_when_purge_fails_then_refresh_token_still_usable(
    consent_client: TestClient, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = login(consent_client)["refresh_token"]

    async def fail(session):
        raise RuntimeError("purge failed")

    monkeypatch.setattr(provider, "purge_dead_rows", fail)
    with pytest.raises(RuntimeError):
        refresh(consent_client, token)
    monkeypatch.setattr(provider, "purge_dead_rows", grants.purge_dead_rows)
    # Nothing was rotated, so the client's retry is not taken for a reuse.
    assert refresh(consent_client, token).status_code == 200


async def test_when_purge_batch_full_then_rest_waits_and_chain_intact(
    consent_client: TestClient, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Foreign keys enforced, as on Postgres: SQLite ignores them by default
    # (one connection: the engine's pool is static).
    async with engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    token = login(consent_client)["refresh_token"]
    for _ in range(2):
        token = refresh(consent_client, token).json()["refresh_token"]
    await set_all(engine, OAuthRefreshToken, revoked_at=utcnow() - timedelta(days=8))
    # Rewritten newest-first, so a purge in table order would delete a row
    # its predecessor's replaced_by_id still names.
    chain = [
        OAuthRefreshToken(**row.model_dump())
        for row in await rows(engine, OAuthRefreshToken, OAuthRefreshToken.expires_at)
    ]
    async with AsyncSession(engine) as session:
        await session.exec(update(OAuthRefreshToken).values(replaced_by_id=None))
        await session.exec(delete(OAuthRefreshToken))
        session.add_all(reversed(chain))
        await session.commit()
    monkeypatch.setattr(grants, "PURGE_BATCH", 1)
    for left in (2, 1, 0):
        async with AsyncSession(engine) as session:
            await grants.purge_dead_rows(session)
        assert len(await rows(engine, OAuthRefreshToken)) == left
