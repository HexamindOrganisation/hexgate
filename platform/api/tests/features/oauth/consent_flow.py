"""The login the consent and code-exchange tests drive, step by step.

The user is the seeded default user; ``OTHER_PROJECT_ID`` is in an org they are not in.
"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import DEFAULT_PROJECT_ID, DEFAULT_USER_ID
from hexgate_api.core.oauth_tokens import mint_access_token
from hexgate_api.features.oauth.authorization import MAX_STATE_LENGTH

PUBLIC_URL = "https://app.hexgate.test"
DASHBOARD_URL = "https://dashboard.hexgate.test"
RESOURCE_URL = "https://app.hexgate.test/mcp"
CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
DOCUMENT = {
    "client_id": CLIENT_ID,
    "client_name": "Claude Code",
    "redirect_uris": [
        "http://localhost/callback",
        "http://127.0.0.1/callback",
        "http://[::1]/callback",
        "http://127.0.0.1/",
        "com.example.app:/callback",
    ],
}
REDIRECT_URI = "http://localhost:33418/callback"
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .decode()
    .rstrip("=")
)
OTHER_ORG_ID = "00000000-0000-0000-0000-0000000000a1"
OTHER_PROJECT_ID = "00000000-0000-0000-0000-0000000000a3"
OTHER_USER_ID = "00000000-0000-0000-0000-0000000000a2"
USER_HEADERS = {"X-Dev-User": DEFAULT_USER_ID}
POST_HEADERS = {**USER_HEADERS, "Origin": DASHBOARD_URL}

ENV = {
    "HEXGATE_PUBLIC_URL": PUBLIC_URL,
    "HEXGATE_MCP_RESOURCE_URL": RESOURCE_URL,
    "HEXGATE_DASHBOARD_URL": DASHBOARD_URL,
    "HEXGATE_OAUTH_ALLOW_INSECURE_CLIENT_IDS": "0",
}


# Each a request /authorize would refuse, so the consent routes must too.
REFUSED_REQUESTS = [
    pytest.param({"redirect_uri": "https://evil.example/callback"}, id="redirect"),
    pytest.param({"redirect_uri": None}, id="no-redirect"),
    pytest.param({"code_challenge_method": "plain"}, id="plain"),
    pytest.param({"code_challenge": "short"}, id="challenge-shape"),
    pytest.param({"scope": "policy:read admin:all"}, id="unknown-scope"),
    pytest.param({"resource": "https://other.example/mcp"}, id="resource"),
    pytest.param({"resource": None}, id="no-resource"),
    pytest.param({"client_id": "https://unknown.example/m.json"}, id="client"),
    pytest.param({"response_type": "token"}, id="response-type"),
    pytest.param({"state": "s" * (MAX_STATE_LENGTH + 1)}, id="long-state"),
    # 171 x "é" is 171 characters but 1026 URL-encoded ("%C3%A9" each).
    pytest.param({"state": "é" * 171}, id="long-encoded-state"),
]


def access_token() -> str:
    """A valid access token for the user: the consent routes must refuse even this."""
    return mint_access_token(
        sub=DEFAULT_USER_ID,
        client_id=CLIENT_ID,
        scopes=["policy:read"],
        projects=[DEFAULT_PROJECT_ID],
        issuer=PUBLIC_URL,
        audience=[RESOURCE_URL, PUBLIC_URL],
    )


def authorization(**overrides: str | None) -> dict[str, str]:
    """The query ``/authorize`` hands the consent page (a test chains the two);
    a None override drops a field."""
    params: dict[str, str | None] = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "policy:read audit:read",
        "state": "xyz",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "resource": RESOURCE_URL,
    } | overrides
    return {k: v for k, v in params.items() if v is not None}


_DEFAULT = object()


def consent(
    client: TestClient,
    projects: list[str] | None | object = _DEFAULT,
    headers: dict[str, str] | None = None,
    **overrides: str | None,
):
    """POST the consent; ``projects=None`` leaves the field out."""
    body = authorization(**overrides)
    if projects is not None:
        body["projects"] = [DEFAULT_PROJECT_ID] if projects is _DEFAULT else projects
    return client.post(
        "/v1/oauth/authorize",
        json=body,
        headers=POST_HEADERS if headers is None else headers,
    )


def code_of(resp) -> str:
    """The code in a consent response's ``redirect_to``."""
    (code,) = parse_qs(urlsplit(resp.json()["redirect_to"]).query)["code"]
    return code


def redeem(client: TestClient, code: str, **overrides: str):
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": VERIFIER,
    } | overrides
    return _token(client, form)


def _token(client: TestClient, form: dict[str, str]):
    return client.post("/v1/oauth/token", data={"client_id": CLIENT_ID} | form)


def login(client: TestClient) -> dict:
    """Consent and redeem the code: the token response of a new login."""
    resp = redeem(client, code_of(consent(client)))
    assert resp.status_code == 200
    return resp.json()


def refresh(client: TestClient, refresh_token: str, **overrides: str):
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    return _token(client, form | overrides)


async def rows(engine: AsyncEngine, model, order_by=None) -> list:
    async with AsyncSession(engine) as session:
        return list((await session.exec(select(model).order_by(order_by))).all())


async def set_all(engine: AsyncEngine, model, **values) -> None:
    """Set ``values`` on every row of ``model``."""
    async with AsyncSession(engine) as session:
        await session.exec(update(model).values(**values))
        await session.commit()
