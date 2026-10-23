"""The authorize and token routes, through the app ``create_app`` builds.

The client's metadata fetch is faked through ``client_cache.fetch_client_metadata``;
everything else is the real wiring, with the SPA catch-all mounted after it as
the lifespan does, so a route mounted under the wrong path, or behind the
catch-all, fails here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hexgate_api.constants import OAUTH_SCOPES
from hexgate_api.core import spa
from hexgate_api.features.oauth import client_cache
from hexgate_api.features.oauth.client_metadata import ClientMetadataError
from hexgate_api.features.oauth.provider import oauth_routes
from hexgate_api.features.oauth.well_known import authorization_server_metadata
from hexgate_api.main import create_app
from hexgate_api.settings import get_settings

PUBLIC_URL = "https://app.hexgate.test"
DASHBOARD_URL = "https://dashboard.hexgate.test"
RESOURCE_URL = "https://app.hexgate.test/mcp"
# Shaped like Claude Code: a metadata-document URL whose document lists a
# portless loopback redirect and no scope, and a redirect on the port it picked.
CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
DOCUMENT = {"client_id": CLIENT_ID, "redirect_uris": ["http://localhost/callback"]}
REDIRECT_URI = "http://localhost:33418/callback"
CODE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
UNVERIFIED_REDIRECT = "https://evil.example/callback"


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HEXGATE_PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setenv("HEXGATE_MCP_RESOURCE_URL", RESOURCE_URL)
    monkeypatch.setenv("HEXGATE_DASHBOARD_URL", DASHBOARD_URL)
    monkeypatch.setenv("HEXGATE_CORS_ORIGINS", "http://localhost:5173")
    # A developer's .env may turn it on, which the remote public_url refuses.
    monkeypatch.setenv("HEXGATE_OAUTH_ALLOW_INSECURE_CLIENT_IDS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fetches(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Serve ``DOCUMENT`` at ``CLIENT_ID`` and fail every other; log each fetch's insecure flag."""
    flags: list[bool] = []

    async def fake_fetch(client_id, *, allow_insecure_localhost=False):
        flags.append(allow_insecure_localhost)
        if client_id != CLIENT_ID:
            raise ClientMetadataError("client_id answered HTTP 404")
        return DOCUMENT

    monkeypatch.setattr(client_cache, "fetch_client_metadata", fake_fetch)
    return flags


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>")
    monkeypatch.setenv("HEXGATE_DASHBOARD_DIST", str(dist))
    app = create_app()
    spa.mount_spa(app)
    return app


@pytest.fixture
def client(fetches, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return TestClient(_app(tmp_path, monkeypatch), follow_redirects=False)


def _authorize(client: TestClient, **overrides: str | None):
    query = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "policy:read audit:read",
        "state": "xyz",
        "code_challenge": CODE_CHALLENGE,
        "code_challenge_method": "S256",
        "resource": RESOURCE_URL,
        **overrides,
    }
    params = {key: value for key, value in query.items() if value is not None}
    return client.get("/v1/oauth/authorize", params=params)


def _location(resp) -> tuple[str, dict[str, list[str]]]:
    url = urlsplit(resp.headers["location"])
    return f"{url.scheme}://{url.netloc}{url.path}", parse_qs(
        url.query, keep_blank_values=True
    )


def test_authorize_happy_path(client: TestClient) -> None:
    resp = _authorize(client)
    assert resp.status_code == 302
    target, query = _location(resp)
    assert target == f"{DASHBOARD_URL}/oauth/consent"
    assert query == {
        "client_id": [CLIENT_ID],
        "redirect_uri": [REDIRECT_URI],
        "scope": ["policy:read audit:read"],
        "state": ["xyz"],
        "code_challenge": [CODE_CHALLENGE],
        "code_challenge_method": ["S256"],
        "resource": [RESOURCE_URL],
    }


def test_when_state_omitted_then_consent_query_omits_it(client: TestClient) -> None:
    resp = _authorize(client, state=None)
    assert resp.status_code == 302
    target, query = _location(resp)
    assert target == f"{DASHBOARD_URL}/oauth/consent"
    assert "state" not in query


def test_when_scope_omitted_then_consent_asks_for_every_scope(
    client: TestClient,
) -> None:
    _, query = _location(_authorize(client, scope=None))
    assert query["scope"] == [" ".join(sorted(OAUTH_SCOPES))]


# Each case would otherwise redirect its error to the client; with no trusted
# client or redirect it must not redirect at all.
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"client_id": "https://unknown.example/m.json"}, id="unknown-client"
        ),
        pytest.param({"client_id": "x" * 3000}, id="long-client-id"),
        pytest.param({"redirect_uri": UNVERIFIED_REDIRECT}, id="unregistered"),
        # The document lists one, which the MCP SDK would otherwise stand in.
        pytest.param({"redirect_uri": None}, id="no-redirect"),
        pytest.param({"redirect_uri": "http://localhost:33418/other"}, id="other-path"),
        pytest.param(
            {"redirect_uri": UNVERIFIED_REDIRECT, "code_challenge_method": "plain"},
            id="plain",
        ),
        pytest.param(
            {"redirect_uri": UNVERIFIED_REDIRECT, "scope": "admin"}, id="scope"
        ),
    ],
)
def test_when_client_or_redirect_unverified_then_400_and_no_redirect(
    client: TestClient, overrides: dict[str, str]
) -> None:
    resp = _authorize(client, **overrides)
    assert resp.status_code == 400
    assert "location" not in resp.headers
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        pytest.param({"code_challenge_method": "plain"}, "invalid_request", id="plain"),
        pytest.param({"code_challenge": None}, "invalid_request", id="no-challenge"),
        pytest.param(
            {"response_type": "token"}, "unsupported_response_type", id="token"
        ),
        pytest.param(
            {"scope": "policy:read admin"}, "invalid_scope", id="unknown-scope"
        ),
        pytest.param(
            {"resource": "https://other.example/mcp"},
            "invalid_request",
            id="other-resource",
        ),
        pytest.param({"resource": f"{RESOURCE_URL}/"}, "invalid_request", id="slash"),
        pytest.param({"resource": None}, "invalid_request", id="no-resource"),
        pytest.param({"code_challenge": ""}, "invalid_request", id="empty-challenge"),
        pytest.param(
            {"code_challenge": CODE_CHALLENGE[:42]},
            "invalid_request",
            id="short-challenge",
        ),
        pytest.param(
            {"code_challenge": f"{CODE_CHALLENGE}A"},
            "invalid_request",
            id="long-challenge",
        ),
        pytest.param(
            {"code_challenge": f"{CODE_CHALLENGE[:42]}="},
            "invalid_request",
            id="padded",
        ),
        pytest.param(
            {"code_challenge": f"{CODE_CHALLENGE[:42]}+"},
            "invalid_request",
            id="bad-char",
        ),
    ],
)
def test_when_query_invalid_then_error_redirected_to_verified_client(
    client: TestClient, overrides: dict[str, str | None], error: str
) -> None:
    resp = _authorize(client, **overrides)
    assert resp.status_code == 302
    target, query = _location(resp)
    assert target == "http://localhost:33418/callback"
    assert query["error"] == [error]
    assert query["state"] == ["xyz"]


def test_when_authorize_posted_then_not_handled_by_sdk(
    client: TestClient, fetches: list[bool]
) -> None:
    # The consent POST takes this path later; the MCP SDK's form-POST authorize must not.
    resp = client.post("/v1/oauth/authorize", data={"client_id": CLIENT_ID})
    assert resp.status_code == 405
    assert fetches == []  # The MCP SDK's handler would have loaded the client.


@pytest.mark.parametrize(
    ("client_id", "status", "error"),
    [
        pytest.param(CLIENT_ID, 400, "invalid_grant", id="known-client"),
        pytest.param(
            "https://unknown.example/m.json", 401, "unauthorized_client", id="unknown"
        ),
    ],
)
@pytest.mark.parametrize(
    "grant",
    [
        pytest.param(
            {"grant_type": "authorization_code", "code": "c", "code_verifier": "v"},
            id="code",
        ),
        pytest.param(
            {"grant_type": "refresh_token", "refresh_token": "r"}, id="refresh"
        ),
    ],
)
def test_when_token_requested_then_clean_error(
    client: TestClient, grant: dict[str, str], client_id: str, status: int, error: str
) -> None:
    resp = client.post("/v1/oauth/token", data={"client_id": client_id, **grant})
    assert resp.status_code == status
    assert resp.json()["error"] == error


async def test_when_sdk_routes_mounted_then_metadata_is_ours(
    client: TestClient,
) -> None:
    resp = client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    assert resp.json() == await authorization_server_metadata()


def test_when_authorize_head_then_405(client: TestClient, fetches: list[bool]) -> None:
    resp = client.head("/v1/oauth/authorize", params={"client_id": CLIENT_ID})
    assert resp.status_code == 405
    assert fetches == []


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("https://evil.example", None),
        ("http://localhost:5173", "http://localhost:5173"),
    ],
)
def test_when_token_posted_cross_origin_then_app_cors_decides(
    client: TestClient, origin: str, allowed: str | None
) -> None:
    # The MCP SDK's own wrapper would answer any origin with "*".
    resp = client.post(
        "/v1/oauth/token",
        data={
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": "r",
        },
        headers={"Origin": origin},
    )
    assert resp.status_code == 400
    assert resp.headers.get("access-control-allow-origin") == allowed


def test_when_client_metadata_refused_then_reason_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="hexgate_api.features.oauth.client_cache")
    _authorize(client, client_id="https://unknown.example/m.json")
    assert (
        "client metadata refused for 'https://unknown.example/m.json': "
        "client_id answered HTTP 404"
    ) in caplog.messages


def test_when_client_refused_repeatedly_then_logged_once(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # The second request is answered from the failure cache, without a fetch.
    caplog.set_level(logging.WARNING, logger="hexgate_api.features.oauth.client_cache")
    for _ in range(3):
        _authorize(client, client_id="https://unknown.example/m.json")
    assert len([m for m in caplog.messages if m.startswith("client metadata")]) == 1


def test_when_client_id_has_newline_then_logged_on_one_line(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="hexgate_api.features.oauth.client_cache")
    _authorize(client, client_id="https://x.example/\nFORGED " + "a" * 1000)
    (message,) = [m for m in caplog.messages if m.startswith("client metadata")]
    assert "\n" not in message
    assert len(message) < 400


def test_when_state_empty_then_consent_query_keeps_it(client: TestClient) -> None:
    _, query = _location(_authorize(client, state=""))
    assert query["state"] == [""]


def test_oauth_routes_happy_path() -> None:
    # Only under /v1/oauth: the MCP SDK's own metadata route, were it kept, would
    # serve its issuer with a trailing slash and advertise client-secret auth.
    assert sorted(route.path for route in oauth_routes()) == [
        "/v1/oauth/authorize",
        "/v1/oauth/token",
    ]


def test_when_sdk_routes_mounted_then_no_registration(client: TestClient) -> None:
    # 405, not 404: the SPA's GET catch-all matches the path. A mounted
    # registration route would answer 201 or 400.
    assert client.post("/v1/oauth/register", json={}).status_code == 405


# Set either way: unset, a developer's .env could turn it on.
@pytest.mark.parametrize(("env", "insecure"), [("1", True), ("0", False)])
def test_when_insecure_client_ids_set_then_cache_fetches_accordingly(
    fetches: list[bool],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: str,
    insecure: bool,
) -> None:
    # The flag is refused unless public_url and dashboard_url are local.
    monkeypatch.setenv("HEXGATE_PUBLIC_URL", "http://localhost:8000")
    monkeypatch.setenv("HEXGATE_DASHBOARD_URL", "http://localhost:5173")
    monkeypatch.setenv("HEXGATE_OAUTH_ALLOW_INSECURE_CLIENT_IDS", env)
    get_settings.cache_clear()
    _authorize(TestClient(_app(tmp_path, monkeypatch), follow_redirects=False))
    assert fetches == [insecure]


def test_when_dashboard_url_has_trailing_slash_then_consent_url_has_one_slash(
    fetches: list[bool], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEXGATE_DASHBOARD_URL", f"{DASHBOARD_URL}/")
    get_settings.cache_clear()
    resp = _authorize(TestClient(_app(tmp_path, monkeypatch), follow_redirects=False))
    target, _ = _location(resp)
    assert target == f"{DASHBOARD_URL}/oauth/consent"
