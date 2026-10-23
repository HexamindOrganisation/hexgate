"""``/consent-info``: the request re-checked as ``/authorize`` checks it."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient

from hexgate_api.constants import (
    DEFAULT_PROJECT_ID,
    OAUTH_SCOPE_DESCRIPTIONS,
    OAUTH_SCOPES,
)
from hexgate_api.features.oauth.authorization import MAX_STATE_LENGTH
from tests.features.oauth.consent_flow import (
    REFUSED_REQUESTS,
    USER_HEADERS,
    access_token,
    authorization,
)


def test_when_state_at_cap_then_accepted(consent_client: TestClient) -> None:
    resp = consent_client.get(
        "/v1/oauth/consent-info",
        params=authorization(state="s" * MAX_STATE_LENGTH),
        headers=USER_HEADERS,
    )
    assert resp.status_code == 200


def test_consent_info_happy_path(consent_client: TestClient) -> None:
    resp = consent_client.get(
        "/v1/oauth/consent-info", params=authorization(), headers=USER_HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_name"] == "Claude Code"
    assert body["client_host"] == "claude.ai"
    assert body["redirect_host"] == "localhost"
    assert [s["name"] for s in body["scopes"]] == ["policy:read", "audit:read"]
    assert all(s["description"] for s in body["scopes"])
    # The user's org only, not the org they are not in.
    assert [p["id"] for p in body["projects"]] == [DEFAULT_PROJECT_ID]


@pytest.mark.parametrize("overrides", REFUSED_REQUESTS)
def test_when_consent_info_query_tampered_then_400(
    consent_client: TestClient, overrides: dict[str, str | None]
) -> None:
    resp = consent_client.get(
        "/v1/oauth/consent-info",
        params=authorization(**overrides),
        headers=USER_HEADERS,
    )
    assert resp.status_code == 400


def test_when_consent_info_with_a_valid_token_then_403(
    consent_client: TestClient,
) -> None:
    headers = {"Authorization": f"Bearer {access_token()}"}
    resp = consent_client.get(
        "/v1/oauth/consent-info", params=authorization(), headers=headers
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "token scope does not cover this route"


def test_when_consent_info_without_session_then_401(consent_client: TestClient) -> None:
    resp = consent_client.get("/v1/oauth/consent-info", params=authorization())
    assert resp.status_code == 401


def test_when_redirect_has_no_host_then_consent_info_shows_it_whole(
    consent_client: TestClient,
) -> None:
    redirect = "com.example.app:/callback"
    resp = consent_client.get(
        "/v1/oauth/consent-info",
        params=authorization(redirect_uri=redirect),
        headers=USER_HEADERS,
    )
    assert resp.json()["redirect_host"] == redirect


@pytest.mark.parametrize(
    ("response_type", "error"),
    [(None, "invalid_request"), ("token", "unsupported_response_type")],
    ids=["missing", "wrong"],
)
def test_when_response_type_refused_then_error_code_matches_authorize(
    consent_client: TestClient, response_type: str | None, error: str
) -> None:
    resp = consent_client.get(
        "/v1/oauth/consent-info",
        params=authorization(response_type=response_type),
        headers=USER_HEADERS,
    )
    assert resp.json()["detail"].startswith(f"{error}:")


async def test_when_authorize_redirects_then_consent_info_accepts_its_query(
    consent_client: TestClient,
) -> None:
    """The page forwards exactly the query /authorize gave it."""
    resp = consent_client.get("/v1/oauth/authorize", params=authorization())
    query = dict(parse_qsl(urlsplit(resp.headers["location"]).query))
    info = consent_client.get(
        "/v1/oauth/consent-info", params=query, headers=USER_HEADERS
    )
    assert info.status_code == 200


def test_scope_descriptions_happy_path() -> None:
    # One per scope, so the consent page never lacks one (a KeyError, a 500).
    assert OAUTH_SCOPE_DESCRIPTIONS.keys() == OAUTH_SCOPES
