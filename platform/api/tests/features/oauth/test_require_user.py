"""The OAuth bearer branch of ``require_user``.

No route declares a scope yet, so a valid token gets a 403 everywhere; the
401s below come from the token or its user, before any scope is looked at.
Tokens are hand-minted: nothing in the API mints one yet.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jwt.utils import base64url_encode
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import DEFAULT_PROJECT_ID, DEFAULT_USER_ID, OAUTH_SCOPE_KEY
from hexgate_api.core import keystore as keystore_mod
from hexgate_api.core.db import get_session
from hexgate_api.core.keystore import FileKeyStore
from hexgate_api.core.oauth_tokens import ACCESS_TOKEN_TTL_SECONDS, mint_access_token
from hexgate_api.deps.identity import require_user
from hexgate_api.main import app
from hexgate_api.models import User
from hexgate_api.seeds.defaults import ensure_default_project
from hexgate_api.settings import get_settings

# A route behind require_user (via require_org_member) that declares no scope.
ROUTE = f"/v1/projects/{DEFAULT_PROJECT_ID}/agents"


@pytest.fixture
async def session_factory(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as bootstrap:
        await ensure_default_project(bootstrap)
    return factory


@pytest.fixture
def oauth_keystore(tmp_path, monkeypatch: pytest.MonkeyPatch) -> FileKeyStore:
    ks = FileKeyStore(base_dir=tmp_path / "keystore")
    ks.ensure_keypair()
    monkeypatch.setattr(keystore_mod, "keystore", ks)
    return ks


@pytest.fixture
def client(session_factory, oauth_keystore) -> TestClient:
    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _mint(**overrides) -> str:
    settings = get_settings()
    kwargs = {
        "sub": DEFAULT_USER_ID,
        "client_id": "https://client.example/meta.json",
        "scopes": ["policy:read"],
        "projects": [DEFAULT_PROJECT_ID],
        "issuer": settings.public_url,
        "audience": settings.mcp_resource_url,
    } | overrides
    return mint_access_token(**kwargs)


def _resign(token: str, sign, **claim_overrides) -> str:
    """``token`` with its claims changed and signed by ``sign``: what mint never makes."""
    header, claims = (
        jwt.get_unverified_header(token),
        jwt.decode(token, options={"verify_signature": False}),
    )
    signing_input = b".".join(
        base64url_encode(json.dumps(part).encode())
        for part in (header, claims | claim_overrides)
    )
    return (signing_input + b"." + base64url_encode(sign(signing_input))).decode()


def _get(client: TestClient, token: str, **headers: str):
    return client.get(ROUTE, headers={"Authorization": f"Bearer {token}", **headers})


def test_when_token_is_valid_then_a_route_with_no_scope_refuses_it(
    client: TestClient,
) -> None:
    """A valid token is still refused: no route declares a scope yet."""
    r = _get(client, _mint())
    assert r.status_code == 403
    assert r.json()["detail"] == "token scope does not cover this route"


@pytest.mark.parametrize(
    "authorization",
    ["Bearer fty_dev_p1_notabiscuit", "Basic Zm9vOmJhcg=="],
    ids=["sdk-key", "basic"],
)
def test_when_header_is_not_an_oauth_bearer_then_require_user_ignores_it(
    client: TestClient, authorization: str
) -> None:
    """An ``fty_`` envelope is left to the SDK path, not parsed as a JWT."""
    r = client.get(
        ROUTE, headers={"Authorization": authorization, "X-Dev-User": DEFAULT_USER_ID}
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"now": int(time.time()) - ACCESS_TOKEN_TTL_SECONDS - 1},
        {"audience": "https://other.example/mcp"},
        {"issuer": "https://other.example"},
    ],
    ids=["expired", "wrong-aud", "wrong-iss"],
)
def test_when_token_does_not_verify_then_401(client: TestClient, overrides) -> None:
    r = _get(client, _mint(**overrides))
    assert r.status_code == 401
    assert r.json()["detail"] == "missing or invalid authentication"


def test_when_signed_by_the_sdk_key_then_401(
    client: TestClient, oauth_keystore: FileKeyStore
) -> None:
    """The SDK root key signs biscuits and sessions; it never passes as the OAuth key."""
    r = _get(client, _resign(_mint(), oauth_keystore.sign))
    assert r.status_code == 401


def test_when_token_names_no_project_then_401(
    client: TestClient, oauth_keystore: FileKeyStore
) -> None:
    """Refused as the MCP server refuses it, not read as "every project"."""
    r = _get(client, _resign(_mint(), oauth_keystore.oauth_sign, projects=[]))
    assert r.status_code == 401


async def test_when_user_is_inactive_then_401(
    client: TestClient, session_factory
) -> None:
    async with session_factory() as session:
        user = await session.get(User, DEFAULT_USER_ID)
        user.is_active = False
        session.add(user)
        await session.commit()
    r = _get(client, _mint())
    assert r.status_code == 401


def test_when_user_does_not_exist_then_401(client: TestClient) -> None:
    r = _get(client, _mint(sub="no-such-user"))
    assert r.status_code == 401


def test_when_token_is_invalid_then_the_dev_header_does_not_rescue_it(
    client: TestClient,
) -> None:
    """A bearer that is ours but fails is a 401, not a fall-through to X-Dev-User."""
    r = _get(client, "not-a-jwt", **{"X-Dev-User": DEFAULT_USER_ID})
    assert r.status_code == 401


def test_when_scheme_is_lowercase_then_the_token_is_still_verified(
    client: TestClient,
) -> None:
    """The scheme is case-insensitive (RFC 9110 §11.1), so ``bearer`` is not ignored."""
    r = client.get(
        ROUTE,
        headers={"Authorization": "bearer not-a-jwt", "X-Dev-User": DEFAULT_USER_ID},
    )
    assert r.status_code == 401


@pytest.mark.parametrize(
    ("mark", "status"),
    [
        (["policy:read", "audit:read"], 200),
        (["policy:read", "agents:read"], 403),
        ("audit:read", 200),
        ([], 403),
    ],
    ids=["list-all-held", "list-one-missing", "str", "empty-list"],
)
def test_when_route_declares_scopes_then_the_token_must_hold_all(
    session_factory, oauth_keystore, mark, status: int
) -> None:
    """A hand-marked route in its own app: no real route declares a scope yet."""
    marked = FastAPI()

    @marked.get("/p/{project_id}", openapi_extra={OAUTH_SCOPE_KEY: mark})
    async def endpoint(project_id: str, user: User = Depends(require_user)) -> str:
        return user.id

    async def override_session():
        async with session_factory() as session:
            yield session

    marked.dependency_overrides[get_session] = override_session
    token = _mint(scopes=["policy:read", "audit:read"])
    r = TestClient(marked).get(
        f"/p/{DEFAULT_PROJECT_ID}", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == status, r.text
