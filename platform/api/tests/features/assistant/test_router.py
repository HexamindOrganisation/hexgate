"""The assistant's token and config routes, through the app, as the dashboard calls them."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import datetime

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import (
    ASSISTANT_CLIENT_ID,
    DEFAULT_ORG_ID,
    DEFAULT_PROJECT_ID,
    DEFAULT_USER_ID,
    OAUTH_SCOPES,
)
from hexgate_api.core import keystore as keystore_mod
from hexgate_api.core.db import get_session
from hexgate_api.core.keystore import FileKeyStore
from hexgate_api.core.oauth_tokens import (
    ACCESS_TOKEN_TTL_SECONDS,
    AccessTokenClaims,
    verify_access_token,
)
from hexgate_api.main import app
from hexgate_api.models import Organization, OrganizationMember, Project, User
from hexgate_api.seeds.defaults import ensure_default_project
from hexgate_api.settings import get_settings
from tests.features.oauth import consent_flow
from tests.features.oauth.conftest import mint
from tests.features.oauth.consent_flow import DASHBOARD_URL, PUBLIC_URL
from tests.features.oauth.consent_flow import RESOURCE_URL as MCP_URL

ASSISTANT_URL = f"{PUBLIC_URL}/v1/assistant"
ENV = consent_flow.ENV | {
    "HEXGATE_ASSISTANT_RESOURCE_URL": ASSISTANT_URL,
    "HEXGATE_ASSISTANT_ENABLED": "true",
}

ADMIN_ID = "00000000-0000-0000-0000-0000000000b1"
MEMBER_ID = "00000000-0000-0000-0000-0000000000b2"
UNVERIFIED_ID = "00000000-0000-0000-0000-0000000000b3"
STRANGER_ID = "00000000-0000-0000-0000-0000000000b4"
# An org the default user is also in, so only the token's projects refuse it.
SECOND_ORG_ID = "00000000-0000-0000-0000-0000000000c1"
SECOND_PROJECT_ID = "00000000-0000-0000-0000-0000000000c2"

READ_SCOPES = {"policy:read", "audit:read", "agents:read"}
P = f"/v1/projects/{DEFAULT_PROJECT_ID}/assistant"


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as seed:
        await ensure_default_project(seed)
        for user_id, role, verified in (
            (ADMIN_ID, "admin", True),
            (MEMBER_ID, "member", True),
            (UNVERIFIED_ID, "owner", False),
        ):
            seed.add(User(id=user_id, email=f"{user_id}@x.test", is_verified=verified))
            seed.add(
                OrganizationMember(user_id=user_id, org_id=DEFAULT_ORG_ID, role=role)
            )
        seed.add(User(id=STRANGER_ID, email="stranger@x.test", is_verified=True))
        seed.add(Organization(id=SECOND_ORG_ID, slug="second", name="Second"))
        seed.add(Project(id=SECOND_PROJECT_ID, org_id=SECOND_ORG_ID, name="second"))
        seed.add(
            OrganizationMember(
                user_id=DEFAULT_USER_ID, org_id=SECOND_ORG_ID, role="owner"
            )
        )
        await seed.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
def client(factory, tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    keys = FileKeyStore(base_dir=tmp_path / "keystore")
    keys.ensure_keypair()
    monkeypatch.setattr(keystore_mod, "keystore", keys)

    async def override_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


async def _set_mode(factory, mode: str) -> None:
    async with factory() as session:
        await session.exec(
            update(Organization)
            .where(Organization.id == DEFAULT_ORG_ID)
            .values(assistant_mode=mode)
        )
        await session.commit()


def _headers(user_id: str = DEFAULT_USER_ID, **extra: str) -> dict[str, str]:
    return {"X-Dev-User": user_id, "Origin": DASHBOARD_URL} | extra


def _token(client: TestClient, user_id: str = DEFAULT_USER_ID):
    return client.post(f"{P}/token", headers=_headers(user_id))


def _claims(token: str, audience: str = ASSISTANT_URL) -> AccessTokenClaims:
    return verify_access_token(token, issuer=PUBLIC_URL, audience=audience)


def test_token_happy_path(client: TestClient) -> None:
    before = int(time.time())
    resp = _token(client)
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["can_write"] is True
    claims = _claims(body["token"])
    assert claims.sub == DEFAULT_USER_ID
    assert claims.client_id == ASSISTANT_CLIENT_ID
    assert claims.scopes == READ_SCOPES | {"policy:apply"}
    assert claims.projects == {DEFAULT_PROJECT_ID}
    assert claims.aud == [ASSISTANT_URL, MCP_URL, PUBLIC_URL]
    assert claims.exp - claims.iat == ACCESS_TOKEN_TTL_SECONDS
    assert before <= claims.iat <= int(time.time())
    assert datetime.fromisoformat(body["expires_at"]).timestamp() == claims.exp


@pytest.mark.parametrize("audience", [MCP_URL, PUBLIC_URL])
def test_when_token_minted_then_the_mcp_and_api_audiences_verify_it(
    client: TestClient, audience: str
) -> None:
    token = _token(client).json()["token"]
    assert _claims(token, audience).client_id == ASSISTANT_CLIENT_ID


def test_when_token_used_on_its_project_then_the_api_accepts_it(
    client: TestClient,
) -> None:
    token = _token(client).json()["token"]
    resp = client.get(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/policy-files",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text


def test_when_token_used_on_another_project_then_403(client: TestClient) -> None:
    """The user owns the second project too: only the token's projects refuse it."""
    token = _token(client).json()["token"]
    resp = client.get(
        f"/v1/projects/{SECOND_PROJECT_ID}/policy-files",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "token does not cover this project"


@pytest.mark.parametrize(
    ("user_id", "mode", "can_write"),
    [
        (DEFAULT_USER_ID, "read_write", True),  # owner
        (ADMIN_ID, "read_write", True),
        (MEMBER_ID, "read_write", False),
        (DEFAULT_USER_ID, "read_only", False),
        (ADMIN_ID, "read_only", False),
        (MEMBER_ID, "read_only", False),
    ],
)
async def test_when_role_and_mode_vary_then_only_an_admin_in_read_write_may_apply(
    client: TestClient, factory, user_id: str, mode: str, can_write: bool
) -> None:
    await _set_mode(factory, mode)
    resp = _token(client, user_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["can_write"] is can_write
    expected = READ_SCOPES | ({"policy:apply"} if can_write else set())
    # Exact: never policy:write, never projects:read.
    assert _claims(resp.json()["token"]).scopes == expected


async def test_when_mode_changes_then_the_next_token_follows(
    client: TestClient, factory
) -> None:
    assert _claims(_token(client).json()["token"]).scopes >= {"policy:apply"}
    await _set_mode(factory, "read_only")
    assert "policy:apply" not in _claims(_token(client).json()["token"]).scopes


async def test_when_mode_is_off_then_403_assistant_off(
    client: TestClient, factory
) -> None:
    await _set_mode(factory, "off")
    resp = _token(client)
    assert resp.status_code == 403
    assert resp.json()["detail"] == {"code": "assistant_off"}


async def test_when_stored_mode_is_unknown_then_403_assistant_mode_unknown(
    client: TestClient, factory
) -> None:
    """Fail closed: a mode this code does not know is refused, never minted."""
    await _set_mode(factory, "read_write_v2")
    resp = _token(client)
    assert resp.status_code == 403
    assert resp.json()["detail"] == {"code": "assistant_mode_unknown"}
    assert "token" not in resp.json()


async def test_when_stored_mode_is_unknown_then_config_says_off(
    client: TestClient, factory
) -> None:
    await _set_mode(factory, "read_write_v2")
    resp = client.get(f"{P}/config", headers={"X-Dev-User": DEFAULT_USER_ID})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "off"
    assert resp.json()["can_write"] is False


def test_when_no_session_and_no_origin_then_401(client: TestClient) -> None:
    """Who is calling is decided before the Origin: 401, not the Origin's 403."""
    assert client.post(f"{P}/token").status_code == 401


def test_when_user_is_unverified_then_403_user_not_verified(client: TestClient) -> None:
    resp = _token(client, UNVERIFIED_ID)
    assert resp.status_code == 403
    assert resp.json()["detail"] == {"code": "user_not_verified"}


def test_when_assistant_disabled_then_token_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEXGATE_ASSISTANT_ENABLED", "false")
    get_settings.cache_clear()
    resp = _token(client)
    assert resp.status_code == 404
    assert resp.json() == {"detail": {"code": "assistant_disabled"}}


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "https://evil.example",
        "https://dashboard.hexgate.test.evil.example",
        "null",
        "https://dashboard.hexgate.test:8443",
        "http://dashboard.hexgate.test",
    ],
    ids=["missing", "other", "suffix", "null", "other-port", "other-scheme"],
)
def test_when_origin_is_not_the_dashboard_then_403(
    client: TestClient, origin: str | None
) -> None:
    headers = {"X-Dev-User": DEFAULT_USER_ID}
    if origin is not None:
        headers["Origin"] = origin
    resp = client.post(f"{P}/token", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "request must come from the dashboard"


@pytest.mark.parametrize(("method", "path"), [("POST", "token"), ("GET", "config")])
@pytest.mark.parametrize("source", ["all-scopes", "assistant"])
def test_when_called_with_an_oauth_token_then_403(
    client: TestClient, method: str, path: str, source: str
) -> None:
    """Unmarked: no token mints another, not even the assistant's own."""
    token = (
        mint(scopes=sorted(OAUTH_SCOPES))
        if source == "all-scopes"
        else _token(client).json()["token"]
    )
    headers = {"Authorization": f"Bearer {token}", "Origin": DASHBOARD_URL}
    resp = client.request(method, f"{P}/{path}", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "token scope does not cover this route"


@pytest.mark.parametrize(("method", "path"), [("POST", "token"), ("GET", "config")])
@pytest.mark.parametrize(
    ("headers", "project_id", "status"),
    [
        pytest.param(
            {"Origin": DASHBOARD_URL}, DEFAULT_PROJECT_ID, 401, id="no-session"
        ),
        pytest.param(_headers(STRANGER_ID), DEFAULT_PROJECT_ID, 403, id="non-member"),
        pytest.param(_headers(), "no-such-project", 404, id="unknown-project"),
    ],
)
def test_when_caller_cannot_see_the_project_then_refused(
    client: TestClient, method, path, headers, project_id, status
) -> None:
    url = f"/v1/projects/{project_id}/assistant/{path}"
    assert client.request(method, url, headers=headers).status_code == status


def test_when_token_minted_then_no_log_line_holds_it(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        token = _token(client).json()["token"]
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "eyJ" not in text
    assert token not in text
    assert f"sub={DEFAULT_USER_ID} project_id={DEFAULT_PROJECT_ID}" in text


@pytest.mark.parametrize(
    ("user_id", "mode", "expected"),
    [
        (DEFAULT_USER_ID, "read_write", {"can_write": True, "verified": True}),
        (MEMBER_ID, "read_write", {"can_write": False, "verified": True}),
        (ADMIN_ID, "read_only", {"can_write": False, "verified": True}),
        (DEFAULT_USER_ID, "off", {"can_write": False, "verified": True}),
        (UNVERIFIED_ID, "read_write", {"can_write": True, "verified": False}),
    ],
)
async def test_config_happy_path(
    client: TestClient, factory, user_id: str, mode: str, expected: dict
) -> None:
    await _set_mode(factory, mode)
    resp = client.get(f"{P}/config", headers={"X-Dev-User": user_id})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enabled": True, "mode": mode} | expected


def test_when_assistant_disabled_then_config_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEXGATE_ASSISTANT_ENABLED", "false")
    get_settings.cache_clear()
    resp = client.get(f"{P}/config", headers={"X-Dev-User": DEFAULT_USER_ID})
    assert resp.json()["enabled"] is False
