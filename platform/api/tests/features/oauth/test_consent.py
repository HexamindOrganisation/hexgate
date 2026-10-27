"""The consent POST, through the app ``create_app`` builds."""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import parse_qs, parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import DEFAULT_PROJECT_ID, DEFAULT_USER_ID, OAUTH_SCOPES
from hexgate_api.models import OAuthAuthorizationCode
from hexgate_api.settings import get_settings
from tests.features.oauth.consent_flow import (
    CHALLENGE,
    CLIENT_ID,
    DASHBOARD_URL,
    OTHER_PROJECT_ID,
    POST_HEADERS,
    REFUSED_REQUESTS,
    REDIRECT_URI,
    RESOURCE_URL,
    USER_HEADERS,
    access_token,
    authorization,
    code_of,
    consent,
)


async def _codes(engine: AsyncEngine) -> list[OAuthAuthorizationCode]:
    async with AsyncSession(engine) as session:
        return list((await session.exec(select(OAuthAuthorizationCode))).all())


async def test_consent_happy_path(consent_client: TestClient, engine) -> None:
    resp = consent(consent_client)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    target = urlsplit(resp.json()["redirect_to"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == REDIRECT_URI
    assert parse_qs(target.query)["state"] == ["xyz"]
    (row,) = await _codes(engine)
    assert row.code_hash != code_of(resp)  # only the hash is stored
    assert row.user_id == DEFAULT_USER_ID
    assert row.client_id == CLIENT_ID
    assert row.redirect_uri == REDIRECT_URI
    assert row.scopes_csv == "policy:read,audit:read"
    assert row.projects_csv == DEFAULT_PROJECT_ID
    assert row.resource == RESOURCE_URL
    assert row.code_challenge == CHALLENGE
    assert row.used_at is None
    lifetime = row.expires_at - row.created_at
    assert abs(lifetime - timedelta(seconds=60)) < timedelta(seconds=1)


async def test_when_state_omitted_then_redirect_has_none(
    consent_client: TestClient,
) -> None:
    resp = consent(consent_client, state=None)
    assert "state" not in parse_qs(urlsplit(resp.json()["redirect_to"]).query)


async def test_when_scope_omitted_then_code_covers_every_scope(
    consent_client: TestClient, engine
) -> None:
    assert consent(consent_client, scope=None).status_code == 200
    (row,) = await _codes(engine)
    assert set(row.scopes_csv.split(",")) == OAUTH_SCOPES


@pytest.mark.parametrize("overrides", REFUSED_REQUESTS)
async def test_when_request_tampered_then_400_and_no_code(
    consent_client: TestClient, engine, overrides: dict[str, str | None]
) -> None:
    resp = consent(consent_client, **overrides)
    assert resp.status_code == 400
    assert await _codes(engine) == []


@pytest.mark.parametrize(
    "projects",
    [
        pytest.param(None, id="missing"),
        pytest.param([], id="empty"),
        pytest.param([OTHER_PROJECT_ID], id="other-org"),
        pytest.param([DEFAULT_PROJECT_ID, OTHER_PROJECT_ID], id="one-outside"),
        pytest.param(["no-such-project"], id="unknown"),
    ],
)
async def test_when_projects_outside_user_orgs_then_400_and_no_code(
    consent_client: TestClient, engine, projects: list[str] | None
) -> None:
    assert consent(consent_client, projects=projects).status_code == 400
    assert await _codes(engine) == []


async def test_when_project_repeated_then_stored_once(
    consent_client: TestClient, engine
) -> None:
    consent(consent_client, projects=[DEFAULT_PROJECT_ID, DEFAULT_PROJECT_ID])
    (row,) = await _codes(engine)
    assert row.projects_csv == DEFAULT_PROJECT_ID


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        pytest.param({"Origin": DASHBOARD_URL}, 401, id="no-session"),
        pytest.param(USER_HEADERS, 403, id="no-origin"),
        pytest.param(
            {**USER_HEADERS, "Origin": "https://evil.example"}, 403, id="other-origin"
        ),
        pytest.param(
            {**USER_HEADERS, "Origin": "https://dashboard.hexgate.test.evil.example"},
            403,
            id="origin-prefix",
        ),
    ],
)
async def test_when_caller_not_dashboard_user_then_refused(
    consent_client: TestClient, engine, headers: dict[str, str], status: int
) -> None:
    assert consent(consent_client, headers=headers).status_code == status
    assert await _codes(engine) == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"data": authorization()}, id="form"),
        pytest.param({"json": [1]}, id="not-an-object"),
        # Valid JSON, but a type a cross-site form can send without a preflight.
        pytest.param(
            {
                "content": json.dumps(
                    authorization() | {"projects": [DEFAULT_PROJECT_ID]}
                ),
                "headers": {**POST_HEADERS, "Content-Type": "text/plain"},
            },
            id="json-as-text",
        ),
    ],
)
async def test_when_body_malformed_then_400(
    consent_client: TestClient, engine, body: dict
) -> None:
    resp = consent_client.post(
        "/v1/oauth/authorize", **({"headers": POST_HEADERS} | body)
    )
    assert resp.status_code == 400
    assert await _codes(engine) == []


async def test_when_json_has_charset_then_accepted(consent_client: TestClient) -> None:
    resp = consent_client.post(
        "/v1/oauth/authorize",
        content=json.dumps(authorization() | {"projects": [DEFAULT_PROJECT_ID]}),
        headers={**POST_HEADERS, "Content-Type": "application/json; charset=utf-8"},
    )
    assert resp.status_code == 200


async def test_when_consent_posted_with_a_valid_token_then_403(
    consent_client: TestClient, engine
) -> None:
    bearer = {"Origin": DASHBOARD_URL, "Authorization": f"Bearer {access_token()}"}
    resp = consent(consent_client, headers=bearer)
    assert (
        resp.status_code == 403
    )  # require_user's default-deny: the route has no scope
    assert resp.json()["detail"] == "token scope does not cover this route"
    assert await _codes(engine) == []


@pytest.mark.parametrize(
    "dashboard_url",
    ["https://Dashboard.hexgate.test", "https://dashboard.hexgate.test:443"],
)
def test_when_dashboard_url_spelled_differently_then_its_origin_still_matches(
    consent_client: TestClient, monkeypatch: pytest.MonkeyPatch, dashboard_url: str
) -> None:
    monkeypatch.setenv("HEXGATE_DASHBOARD_URL", dashboard_url)
    get_settings.cache_clear()
    assert consent(consent_client).status_code == 200


@pytest.mark.parametrize(
    "dashboard_url",
    [None, "https://dashboard.hexgate.test:99999"],
    ids=["ok", "bad-port"],
)
def test_when_origin_is_null_then_403(
    consent_client: TestClient, monkeypatch: pytest.MonkeyPatch, dashboard_url
) -> None:
    if dashboard_url is not None:
        monkeypatch.setenv("HEXGATE_DASHBOARD_URL", dashboard_url)
        get_settings.cache_clear()
    headers = {**USER_HEADERS, "Origin": "null"}
    assert consent(consent_client, headers=headers).status_code == 403


async def test_when_authorize_redirects_then_consent_accepts_its_query(
    consent_client: TestClient,
) -> None:
    """The page posts back exactly the query /authorize gave it."""
    resp = consent_client.get("/v1/oauth/authorize", params=authorization())
    query = dict(parse_qsl(urlsplit(resp.headers["location"]).query))
    body = query | {"projects": [DEFAULT_PROJECT_ID]}
    posted = consent_client.post("/v1/oauth/authorize", json=body, headers=POST_HEADERS)
    assert posted.status_code == 200
