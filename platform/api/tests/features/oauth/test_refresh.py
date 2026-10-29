"""Rotating a refresh token at ``/token``, and revoking one at ``/revoke``.

Each refresh revokes the token it used; presenting a revoked one means it
leaked, so the whole login is revoked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import TokenError
from sqlalchemy import event, update
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import DEFAULT_PROJECT_ID
from hexgate_api.core.oauth_tokens import verify_access_token
from hexgate_api.features.oauth import client_cache
from hexgate_api.features.oauth.grants import as_utc
from hexgate_api.models import OAuthRefreshToken
from tests.features.oauth.consent_flow import (
    CLIENT_ID,
    DOCUMENT,
    PUBLIC_URL,
    login,
    refresh,
    rows,
    set_all,
)


async def _tokens(engine) -> list[OAuthRefreshToken]:
    return await rows(engine, OAuthRefreshToken, OAuthRefreshToken.expires_at)


def _revoke(client: TestClient, token: str, **overrides: str):
    # No client_secret: a public client sends none.
    form = {"token": token, "client_id": CLIENT_ID} | overrides
    return client.post("/v1/oauth/revoke", data=form)


def _claims(body: dict):
    return verify_access_token(
        body["access_token"], issuer=PUBLIC_URL, audience=PUBLIC_URL
    )


async def test_refresh_happy_path(consent_client: TestClient, engine) -> None:
    first = login(consent_client)
    resp = refresh(consent_client, first["refresh_token"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["refresh_token"] != first["refresh_token"]
    assert body["scope"] == "policy:read audit:read"
    # Copied, or every grant would refuse every project after its first rotation.
    assert _claims(body).projects == {DEFAULT_PROJECT_ID}
    old, new = await _tokens(engine)
    assert old.revoked_at is not None
    assert old.replaced_by_id == new.id
    assert new.revoked_at is None
    for field in (
        "family_id",
        "user_id",
        "client_id",
        "client_name",
        "scopes_csv",
        "projects_csv",
        "resource",
        "authorization_code_id",
        "created_at",
    ):
        assert getattr(new, field) == getattr(old, field), field
    lifetime = new.expires_at - new.last_used_at
    assert abs(lifetime - timedelta(days=30)) < timedelta(seconds=1)


async def _rotated_ago(engine, age: timedelta) -> None:
    """Move every revoked row's revocation ``age`` into the past."""
    async with AsyncSession(engine) as session:
        await session.exec(
            update(OAuthRefreshToken)
            .where(OAuthRefreshToken.revoked_at.is_not(None))
            .values(revoked_at=datetime.now(timezone.utc) - age)
        )
        await session.commit()


LONG_AGO = timedelta(minutes=1)  # past the reuse window


async def test_when_rotated_token_reused_then_whole_login_revoked(
    consent_client: TestClient, engine, caplog: pytest.LogCaptureFixture
) -> None:
    first = login(consent_client)
    second = refresh(consent_client, first["refresh_token"]).json()
    await _rotated_ago(engine, LONG_AGO)
    resp = refresh(consent_client, first["refresh_token"])
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
    assert all(row.revoked_at is not None for row in await _tokens(engine))
    resp = refresh(consent_client, second["refresh_token"])
    assert resp.json()["error"] == "invalid_grant"
    (record,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert record.getMessage().startswith("oauth refresh reuse: family=")
    assert record.getMessage().endswith("revoked=1")


@pytest.mark.parametrize(
    ("age", "login_lives"),
    [
        pytest.param(timedelta(seconds=29), True, id="inside-window"),
        pytest.param(timedelta(seconds=31), False, id="past-window"),
    ],
)
async def test_when_rotated_token_presented_again_then_window_decides(
    consent_client: TestClient,
    engine,
    caplog: pytest.LogCaptureFixture,
    age: timedelta,
    login_lives: bool,
) -> None:
    first = login(consent_client)["refresh_token"]
    second = refresh(consent_client, first).json()["refresh_token"]
    await _rotated_ago(engine, age)
    assert refresh(consent_client, first).json()["error"] == "invalid_grant"
    assert (refresh(consent_client, second).status_code == 200) is login_lives
    # One line each: the retry, or the one revoke that revoked something.
    lines = [
        r.message.split(":")[0] for r in caplog.records if "oauth refresh" in r.message
    ]
    retry, reuse = "oauth refresh retry within the reuse window", "oauth refresh reuse"
    assert lines == ([retry] if login_lives else [reuse])


async def test_when_thief_rotated_first_then_expired_reuse_still_revokes_login(
    consent_client: TestClient, engine
) -> None:
    users = login(consent_client)["refresh_token"]
    thief = refresh(consent_client, users).json()["refresh_token"]
    old, _ = await _tokens(engine)
    await _rotated_ago(engine, timedelta(days=2))
    # Past its own expiry: the MCP SDK's expiry check is skipped for a revoked row.
    async with AsyncSession(engine) as session:
        await session.exec(
            update(OAuthRefreshToken)
            .where(OAuthRefreshToken.id == old.id)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        )
        await session.commit()
    assert refresh(consent_client, users).json()["error"] == "invalid_grant"
    assert refresh(consent_client, thief).json()["error"] == "invalid_grant"


async def test_when_rotation_commits_during_reuse_revoke_then_it_is_revoked_too(
    consent_client: TestClient, engine
) -> None:
    first = login(consent_client)["refresh_token"]
    refresh(consent_client, first)
    await _rotated_ago(engine, LONG_AGO)
    old, _ = await _tokens(engine)

    # On Postgres, a concurrent refresh committing right after the revoke's
    # UPDATE leaves a successor that UPDATE never saw; SQLite serialises
    # writers, so the commit is played here, just after that UPDATE.
    played = []

    def rotation_commits(conn, cursor, statement, *args) -> None:
        if not played and statement.startswith(
            "UPDATE oauth_refresh_token SET revoked_at"
        ):
            played.append(statement)
            # Its own cursor: this one still holds the UPDATE's RETURNING rows.
            conn.connection.dbapi_connection.cursor().execute(
                "INSERT INTO oauth_refresh_token (id, token_hash, user_id,"
                " family_id, client_id, client_name, scopes_csv, projects_csv,"
                " resource, created_at, expires_at) SELECT 'late', 'late', user_id,"
                " family_id, client_id, client_name, scopes_csv, projects_csv,"
                " resource, created_at, expires_at FROM oauth_refresh_token"
                f" WHERE id = '{old.id}'"
            )

    event.listen(engine.sync_engine, "after_cursor_execute", rotation_commits)
    try:
        assert refresh(consent_client, first).json()["error"] == "invalid_grant"
    finally:
        event.remove(engine.sync_engine, "after_cursor_execute", rotation_commits)
    tokens = await _tokens(engine)
    assert len(tokens) == 3
    assert all(row.revoked_at is not None for row in tokens)


async def test_when_reused_then_other_logins_untouched(
    consent_client: TestClient, engine
) -> None:
    first, other = login(consent_client), login(consent_client)
    refresh(consent_client, first["refresh_token"])
    await _rotated_ago(engine, LONG_AGO)
    refresh(consent_client, first["refresh_token"])
    assert refresh(consent_client, other["refresh_token"]).status_code == 200


async def test_when_two_refreshes_race_then_one_wins_and_login_lives(
    consent_client: TestClient, engine
) -> None:
    token = login(consent_client)["refresh_token"]
    provider = consent_client.app.state.oauth_provider
    client = await provider.get_client(CLIENT_ID)
    # Both loaded before either rotates, as two concurrent requests would.
    a = await provider.load_refresh_token(client, token)
    b = await provider.load_refresh_token(client, token)
    won = await provider.exchange_refresh_token(client, a, a.scopes)
    with pytest.raises(TokenError) as exc:
        await provider.exchange_refresh_token(client, b, b.scopes)
    assert exc.value.error == "invalid_grant"
    assert len(await _tokens(engine)) == 2  # the loser wrote no successor
    assert refresh(consent_client, won.refresh_token).status_code == 200


async def test_when_login_nears_max_age_then_rotation_ends_with_it(
    consent_client: TestClient, engine
) -> None:
    token = login(consent_client)["refresh_token"]
    created = datetime.now(timezone.utc) - timedelta(days=89)
    await set_all(engine, OAuthRefreshToken, created_at=created)
    assert refresh(consent_client, token).status_code == 200
    (new,) = [row for row in await _tokens(engine) if row.revoked_at is None]
    # A day left, not thirty: the login ends 90 days after the consent.
    end = created + timedelta(days=90)
    assert abs(as_utc(new.expires_at) - end) < timedelta(seconds=1)


async def test_when_rotation_loses_to_a_revoked_login_then_not_a_retry(
    consent_client: TestClient, engine, caplog: pytest.LogCaptureFixture
) -> None:
    token = login(consent_client)["refresh_token"]
    provider = consent_client.app.state.oauth_provider
    client = await provider.get_client(CLIENT_ID)
    loaded = await provider.load_refresh_token(client, token)
    await provider.revoke_token(loaded)  # the user logs the client out meanwhile
    with pytest.raises(TokenError) as exc:
        await provider.exchange_refresh_token(client, loaded, loaded.scopes)
    assert exc.value.error_description == "refresh token was already used"
    assert not [r for r in caplog.records if "oauth refresh" in r.message]


@pytest.mark.parametrize(
    ("left", "error"),
    [
        pytest.param(timedelta(seconds=5), None, id="live"),
        pytest.param(-timedelta(seconds=5), "invalid_grant", id="expired"),
    ],
)
async def test_when_refresh_token_reaches_ttl_then_invalid_grant(
    consent_client: TestClient, engine, left: timedelta, error: str | None
) -> None:
    token = login(consent_client)["refresh_token"]
    # Against the real clock, which the MCP SDK compares expiry to.
    now = datetime.now(timezone.utc)
    await set_all(engine, OAuthRefreshToken, expires_at=now + left)
    assert refresh(consent_client, token).json().get("error") == error


def test_when_refresh_asks_wider_scope_then_invalid_scope(
    consent_client: TestClient,
) -> None:
    token = login(consent_client)["refresh_token"]
    resp = refresh(consent_client, token, scope="policy:read policy:write")
    assert resp.json()["error"] == "invalid_scope"
    assert refresh(consent_client, token).status_code == 200  # not spent


def test_when_refresh_asks_narrower_scope_then_row_scope_kept(
    consent_client: TestClient,
) -> None:
    token = login(consent_client)["refresh_token"]
    body = refresh(consent_client, token, scope="policy:read").json()
    assert body["scope"] == "policy:read audit:read"
    assert _claims(body).scopes == {"policy:read", "audit:read"}


def test_when_refresh_token_unknown_then_invalid_grant(
    consent_client: TestClient,
) -> None:
    assert refresh(consent_client, "not-a-token").json()["error"] == "invalid_grant"


async def test_revoke_happy_path(consent_client: TestClient, engine) -> None:
    first = login(consent_client)
    second = refresh(consent_client, first["refresh_token"]).json()
    resp = _revoke(consent_client, second["refresh_token"])
    assert resp.status_code == 200
    assert all(row.revoked_at is not None for row in await _tokens(engine))
    assert refresh(consent_client, second["refresh_token"]).status_code == 400


async def test_when_revoked_token_unknown_then_200_and_nothing_revoked(
    consent_client: TestClient, engine
) -> None:
    login(consent_client)
    assert _revoke(consent_client, "not-a-token").status_code == 200
    assert all(row.revoked_at is None for row in await _tokens(engine))


async def test_when_access_token_revoked_then_200_and_nothing_revoked(
    consent_client: TestClient, engine
) -> None:
    access_token = login(consent_client)["access_token"]
    assert _revoke(consent_client, access_token).status_code == 200
    assert all(row.revoked_at is None for row in await _tokens(engine))


async def test_when_another_client_revokes_token_then_200_and_nothing_revoked(
    consent_client: TestClient, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = "https://other.example/meta.json"

    async def fetch(client_id, *, allow_insecure_localhost=False):
        return DOCUMENT | {"client_id": client_id}

    monkeypatch.setattr(client_cache, "fetch_client_metadata", fetch)
    token = login(consent_client)["refresh_token"]
    assert _revoke(consent_client, token, client_id=other).status_code == 200
    assert all(row.revoked_at is None for row in await _tokens(engine))


def test_when_revoke_has_no_token_then_invalid_request(
    consent_client: TestClient,
) -> None:
    resp = consent_client.post("/v1/oauth/revoke", data={"client_id": CLIENT_ID})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


def test_when_revoke_client_unknown_then_401(consent_client: TestClient) -> None:
    token = login(consent_client)["refresh_token"]
    resp = _revoke(consent_client, token, client_id="https://unknown.example/m.json")
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_client"  # RFC 6749 section 5.2
