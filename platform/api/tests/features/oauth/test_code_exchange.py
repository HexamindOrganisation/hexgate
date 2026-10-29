"""Redeeming the consent's code at ``/token``.

Loading the code spends it, before the MCP SDK compares the client, redirect
and verifier, so each of those refusals leaves the code unusable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import TokenError

from hexgate_api.constants import DEFAULT_PROJECT_ID, DEFAULT_USER_ID
from hexgate_api.core.oauth_tokens import verify_access_token
from hexgate_api.features.oauth import grants
from hexgate_api.models import OAuthAuthorizationCode, OAuthRefreshToken
from tests.features.oauth.consent_flow import (
    CLIENT_ID,
    PUBLIC_URL,
    REDIRECT_URI,
    RESOURCE_URL,
    code_of,
    consent,
    redeem,
    rows,
)


def _login(client: TestClient, **overrides: str) -> str:
    resp = consent(client, **overrides)
    assert resp.status_code == 200
    return code_of(resp)


async def test_code_exchange_happy_path(consent_client: TestClient, engine) -> None:
    resp = redeem(consent_client, _login(consent_client))
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 600
    assert body["scope"] == "policy:read audit:read"
    # Verified as each service does it: the API against its URL, the MCP server against its own.
    for audience in (PUBLIC_URL, RESOURCE_URL):
        claims = verify_access_token(
            body["access_token"], issuer=PUBLIC_URL, audience=audience
        )
        assert claims.aud == [RESOURCE_URL, PUBLIC_URL]
    assert claims.sub == DEFAULT_USER_ID
    assert claims.client_id == CLIENT_ID
    assert claims.scopes == {"policy:read", "audit:read"}
    assert claims.projects == {DEFAULT_PROJECT_ID}
    assert claims.exp - claims.iat == 600
    assert jwt.get_unverified_header(body["access_token"])["typ"] == "at+jwt"
    (refresh,) = await rows(engine, OAuthRefreshToken)
    assert refresh.token_hash != body["refresh_token"]  # only the hash is stored
    assert refresh.family_id == refresh.id
    assert refresh.user_id == DEFAULT_USER_ID
    assert refresh.client_id == CLIENT_ID
    assert refresh.client_name == "Claude Code"
    assert refresh.scopes_csv == "policy:read,audit:read"
    assert refresh.projects_csv == DEFAULT_PROJECT_ID
    assert refresh.resource == RESOURCE_URL
    assert refresh.revoked_at is None
    lifetime = refresh.expires_at - refresh.created_at
    assert abs(lifetime - timedelta(days=30)) < timedelta(seconds=1)
    (code,) = await rows(engine, OAuthAuthorizationCode)
    assert code.used_at is not None


async def test_when_code_redeemed_twice_then_invalid_grant(
    consent_client: TestClient, engine, caplog: pytest.LogCaptureFixture
) -> None:
    code = _login(consent_client)
    assert redeem(consent_client, code).status_code == 200
    resp = redeem(consent_client, code)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
    # The replay means the code leaked: what it issued is revoked too.
    (refresh,) = await rows(engine, OAuthRefreshToken)
    assert refresh.revoked_at is not None
    (record,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert record.getMessage().startswith("oauth code replay: code=")
    assert code not in record.getMessage()  # the row's id, never the code
    assert record.getMessage().endswith("revoked=1")


async def test_when_code_replayed_before_first_redemption_issues_then_refused(
    consent_client: TestClient, engine
) -> None:
    code = _login(consent_client)
    provider = consent_client.app.state.oauth_provider
    client = await provider.get_client(CLIENT_ID)
    granted = await provider.load_authorization_code(client, code)
    # The replay lands between the first redemption spending the code and
    # issuing its tokens, when there is nothing yet to revoke.
    assert await provider.load_authorization_code(client, code) is None
    with pytest.raises(TokenError) as exc:
        await provider.exchange_authorization_code(client, granted)
    assert exc.value.error == "invalid_grant"
    assert await rows(engine, OAuthRefreshToken) == []


async def test_when_code_replayed_then_only_its_own_login_revoked(
    consent_client: TestClient, engine
) -> None:
    first, second = _login(consent_client), _login(consent_client)
    assert redeem(consent_client, first).status_code == 200
    assert redeem(consent_client, second).status_code == 200
    redeem(consent_client, first)
    by_code = {
        r.authorization_code_id: r for r in await rows(engine, OAuthRefreshToken)
    }
    codes = {r.code_hash: r.id for r in await rows(engine, OAuthAuthorizationCode)}
    assert by_code[codes[grants._hash(first)]].revoked_at is not None
    assert by_code[codes[grants._hash(second)]].revoked_at is None


def test_when_code_unknown_then_invalid_grant(consent_client: TestClient) -> None:
    resp = redeem(consent_client, "not-a-code")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


@pytest.mark.parametrize(
    ("age", "error"),
    [
        pytest.param(
            timedelta(seconds=60) - timedelta(microseconds=1), None, id="live"
        ),
        pytest.param(timedelta(seconds=60), "invalid_grant", id="at-expiry"),
    ],
)
def test_when_code_reaches_ttl_then_invalid_grant(
    consent_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    age: timedelta,
    error: str | None,
) -> None:
    # Real time, not a fixed date: the MCP SDK checks expiry against the clock too.
    t0 = datetime.now(timezone.utc)
    monkeypatch.setattr(grants, "utcnow", lambda: t0)
    code = _login(consent_client)
    monkeypatch.setattr(grants, "utcnow", lambda: t0 + age)
    assert redeem(consent_client, code).json().get("error") == error


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        pytest.param({"code_verifier": "x" * 43}, "invalid_grant", id="wrong-verifier"),
        pytest.param(
            {"redirect_uri": f"{REDIRECT_URI}?x=1"},
            "invalid_request",
            id="redirect-query",
        ),
        pytest.param(
            {"redirect_uri": "http://localhost:33419/callback"},
            "invalid_request",
            id="redirect-port",
        ),
    ],
)
def test_when_redemption_mismatches_then_refused_and_code_burned(
    consent_client: TestClient, overrides: dict[str, str], error: str
) -> None:
    code = _login(consent_client)
    assert redeem(consent_client, code, **overrides).json()["error"] == error
    resp = redeem(consent_client, code)  # now with every field right
    assert resp.json()["error"] == "invalid_grant"


def test_when_client_unknown_then_401_before_the_code_is_read(
    consent_client: TestClient,
) -> None:
    # The MCP SDK authenticates the client before it loads the code, so an
    # unknown client_id never reaches (or spends) it.
    code = _login(consent_client)
    resp = redeem(consent_client, code, client_id="https://unknown.example/m.json")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized_client"
    assert redeem(consent_client, code).status_code == 200


# Each pair is one URL as pydantic normalises it (the MCP SDK's /token does), in both orders.
_EQUAL_REDIRECTS = [
    ("http://LOCALHOST:33418/callback", REDIRECT_URI),
    ("http://127.1:5/callback", "http://127.0.0.1:5/callback"),
    ("http://[0:0:0:0:0:0:0:1]:5/callback", "http://[::1]:5/callback"),
    ("http://127.0.0.1:5", "http://127.0.0.1:5/"),
]


@pytest.mark.parametrize(
    ("at_consent", "at_token"),
    _EQUAL_REDIRECTS + [(b, a) for a, b in _EQUAL_REDIRECTS],
)
def test_when_redirect_uri_normalises_equal_then_accepted(
    consent_client: TestClient, at_consent: str, at_token: str
) -> None:
    code = _login(consent_client, redirect_uri=at_consent)
    assert redeem(consent_client, code, redirect_uri=at_token).status_code == 200
