"""The user's live logins at ``/v1/oauth/grants``."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from hexgate_api.constants import DEFAULT_PROJECT_ID
from hexgate_api.models import OAuthRefreshToken, utcnow
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


async def test_when_grant_scope_left_the_registry_then_listed_by_name(
    consent_client: TestClient, engine
) -> None:
    login(consent_client)
    await set_all(engine, OAuthRefreshToken, scopes_csv="policy:read,retired:scope")
    (grant,) = _grants(consent_client).json()
    assert grant["scopes"][1] == {
        "name": "retired:scope",
        "description": "retired:scope",
    }
