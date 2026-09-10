"""Tests for the API-key CRUD routes (`/v1/projects/{id}/tokens`).

Covers mint / list / revoke through the actual HTTP router — the existing
suite only ever calls `mint_api_key()` directly as a helper to get a token
for *other* features' tests, so the router itself (and `list_api_keys`)
had no coverage of its own. Fixtures mirror `test_projects.py`.
"""

from __future__ import annotations

import asyncio

from biscuit_auth import AuthorizerBuilder, Rule
from fastapi.testclient import TestClient
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import DEFAULT_PROJECT_ID
from hexgate_api.core import keystore as keystore_mod
from hexgate_api.core.biscuits import parse_envelope, verify_token
from hexgate_api.core.keystore import FileKeyStore
from hexgate_api.features.tokens.service import mask_secret, mint_api_key
from hexgate_api.main import app
from hexgate_api.models import ApiKey
from hexgate_api.seeds.defaults import ensure_default_project


# ---------------------------------------------------------------------------
# Fixtures — mirror test_projects.py
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as bootstrap:
        await ensure_default_project(bootstrap)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def client(session_factory, tmp_path) -> TestClient:
    from hexgate_api.core.db import get_session
    from hexgate_api.core.keystore import FileKeyStore

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    original_keystore = keystore_mod.keystore
    keystore_mod.keystore = FileKeyStore(base_dir=tmp_path / "keystore")
    keystore_mod.keystore.ensure_keypair()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        keystore_mod.keystore = original_keystore


def _signup_and_login(client: TestClient, email: str, password: str) -> None:
    """Register + log in; cookie persists on the client for the next call."""
    r = client.post(
        "/v1/auth/register",
        json={"email": email, "password": password},
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/v1/auth/cookie/login",
        data={"username": email, "password": password},
    )
    assert r.status_code == 204, r.text


def _signup_with_project(client: TestClient, email: str) -> str:
    """Sign up, log in, create a project in the user's default org.

    Returns the project id — every tokens endpoint is project-scoped.
    """
    _signup_and_login(client, email, "correcthorsebattery")
    org_id = client.get("/v1/orgs").json()[0]["id"]
    r = client.post(f"/v1/orgs/{org_id}/projects", json={"name": "tokens-project"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _read_token(session_factory, token_id: str) -> ApiKey | None:
    """Read the raw row from a sync test, bypassing the service filters.

    ``run_until_complete`` on the ambient loop rather than ``asyncio.run``:
    the in-memory engine belongs to the fixture's loop, and a fresh one would
    not see it. Same shape as ``test_ws_serve.py``.
    """

    async def _get() -> ApiKey | None:
        async with session_factory() as session:
            return await session.get(ApiKey, token_id)

    return asyncio.get_event_loop().run_until_complete(_get())


# ---------------------------------------------------------------------------
# POST /v1/projects/{id}/tokens
# ---------------------------------------------------------------------------


def test_mint_token_happy_path(client: TestClient) -> None:
    pid = _signup_with_project(client, "minter@example.com")

    r = client.post(f"/v1/projects/{pid}/tokens", json={"name": "ci-deploy"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "ci-deploy"
    assert body["id"]
    assert body["full"].startswith("fty_test_")  # env defaults to "test"
    assert body["masked"] != body["full"]  # never echoes the secret unmasked
    assert body["scopes"] == ["mint_user_token", "read_audit"]  # schema default


def test_mint_token_when_caller_is_not_an_org_member_then_status_is_403(
    client: TestClient,
) -> None:
    pid = _signup_with_project(client, "ownerG@example.com")
    client.cookies.clear()
    _signup_and_login(client, "strangerG@example.com", "correcthorsebattery")

    r = client.post(f"/v1/projects/{pid}/tokens", json={"name": "sneaky"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# GET /v1/projects/{id}/tokens
# ---------------------------------------------------------------------------


def test_list_tokens_happy_path(client: TestClient) -> None:
    pid = _signup_with_project(client, "lister2@example.com")
    client.post(f"/v1/projects/{pid}/tokens", json={"name": "key-a"})
    client.post(f"/v1/projects/{pid}/tokens", json={"name": "key-b"})

    r = client.get(f"/v1/projects/{pid}/tokens")
    assert r.status_code == 200
    items = r.json()
    names = {item["name"] for item in items}
    assert names == {"key-a", "key-b"}
    for item in items:
        assert "full" not in item  # the list view never returns the raw secret
        assert item["masked"]


def test_list_tokens_when_caller_is_not_an_org_member_then_status_is_403(
    client: TestClient,
) -> None:
    pid = _signup_with_project(client, "ownerH@example.com")
    client.cookies.clear()
    _signup_and_login(client, "strangerH@example.com", "correcthorsebattery")

    r = client.get(f"/v1/projects/{pid}/tokens")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /v1/projects/{id}/tokens/{token_id}
# ---------------------------------------------------------------------------


def test_revoke_token_happy_path(client: TestClient) -> None:
    pid = _signup_with_project(client, "revoker@example.com")
    token_id = client.post(
        f"/v1/projects/{pid}/tokens", json={"name": "throwaway"}
    ).json()["id"]

    r = client.delete(f"/v1/projects/{pid}/tokens/{token_id}")
    assert r.status_code == 204

    r = client.get(f"/v1/projects/{pid}/tokens")
    assert r.json() == []


def test_revoke_token_when_token_already_deleted_then_status_is_404(
    client: TestClient,
) -> None:
    pid = _signup_with_project(client, "doublerevoke@example.com")
    token_id = client.post(f"/v1/projects/{pid}/tokens", json={"name": "once"}).json()[
        "id"
    ]
    client.delete(f"/v1/projects/{pid}/tokens/{token_id}")

    r = client.delete(f"/v1/projects/{pid}/tokens/{token_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Soft delete — the row is retained as the audit record, and every read path
# must stop resolving it. Without the filters, flipping the delete to a stamp
# is a revocation bypass on HTTP, WebSocket and OTLP ingest alike.
# ---------------------------------------------------------------------------


def test_revoke_token_when_revoked_then_the_row_is_retained_with_the_actor(
    client: TestClient, session_factory
) -> None:
    pid = _signup_with_project(client, "audittrail@example.com")
    me_id = client.get("/v1/users/me").json()["id"]
    token_id = client.post(
        f"/v1/projects/{pid}/tokens", json={"name": "traced"}
    ).json()["id"]

    assert client.delete(f"/v1/projects/{pid}/tokens/{token_id}").status_code == 204

    row = _read_token(session_factory, token_id)
    assert row is not None, "the row is the audit record; a revoke must not delete it"
    assert row.revoked_at is not None
    assert row.revoked_by_user_id == me_id


def test_revoke_token_when_revoked_twice_then_the_first_stamp_is_kept(
    client: TestClient, session_factory
) -> None:
    """A repeat revoke 404s and must not move the timestamp or the actor.

    The second call losing the race is the whole point of the conditional
    UPDATE in ``revoke_api_key``: overwriting the stamp would destroy the audit
    fact the retained row exists to carry.
    """
    pid = _signup_with_project(client, "twicerevoked@example.com")
    me_id = client.get("/v1/users/me").json()["id"]
    token_id = client.post(f"/v1/projects/{pid}/tokens", json={"name": "twice"}).json()[
        "id"
    ]

    assert client.delete(f"/v1/projects/{pid}/tokens/{token_id}").status_code == 204
    first_stamp = _read_token(session_factory, token_id).revoked_at

    assert client.delete(f"/v1/projects/{pid}/tokens/{token_id}").status_code == 404

    row = _read_token(session_factory, token_id)
    assert row.revoked_at == first_stamp
    assert row.revoked_by_user_id == me_id


def test_revoke_token_when_revoked_then_the_secret_is_masked(
    client: TestClient, session_factory
) -> None:
    """The audit row keeps who/when, not a usable credential.

    A DB dump -- or a rollback to code that does not filter on ``revoked_at``
    -- would otherwise hand back working keys for every revocation ever made.
    """
    pid = _signup_with_project(client, "maskedonrevoke@example.com")
    minted = client.post(f"/v1/projects/{pid}/tokens", json={"name": "masked"}).json()
    token_id = minted["id"]
    full_token = minted["full"]

    assert client.delete(f"/v1/projects/{pid}/tokens/{token_id}").status_code == 204

    stored = _read_token(session_factory, token_id).secret
    assert stored != full_token
    assert stored == minted["masked"] == mask_secret(full_token)
    # The biscuit payload is what authenticates; the envelope prefix is public.
    assert parse_envelope(full_token)[2] not in stored


def test_me_key_when_the_token_is_revoked_then_status_is_401(
    client: TestClient,
) -> None:
    """``GET /v1/me/key`` — the introspection surface (router.py)."""
    pid = _signup_with_project(client, "introspect@example.com")
    minted = client.post(f"/v1/projects/{pid}/tokens", json={"name": "introspect"})
    full, token_id = minted.json()["full"], minted.json()["id"]
    headers = {"Authorization": f"Bearer {full}"}
    assert client.get("/v1/me/key", headers=headers).status_code == 200

    client.delete(f"/v1/projects/{pid}/tokens/{token_id}")

    assert client.get("/v1/me/key", headers=headers).status_code == 401


def test_require_project_when_the_token_is_revoked_then_status_is_401(
    client: TestClient,
) -> None:
    """A bearer-only SDK route — ``require_project`` (deps/tokens.py)."""
    pid = _signup_with_project(client, "sdkroute@example.com")
    minted = client.post(f"/v1/projects/{pid}/tokens", json={"name": "sdk"})
    full, token_id = minted.json()["full"], minted.json()["id"]
    headers = {"Authorization": f"Bearer {full}"}
    assert client.get("/v1/agents/nonexistent", headers=headers).status_code == 404

    client.delete(f"/v1/projects/{pid}/tokens/{token_id}")

    assert client.get("/v1/agents/nonexistent", headers=headers).status_code == 401


async def test_optional_api_key_when_the_token_is_revoked_then_it_raises_401(
    client: TestClient, session_factory
) -> None:
    """``optional_api_key`` has no route wired to it yet, so it is gated here
    directly — it is the third caller of ``_validate_sdk_token`` and would
    otherwise be the one surface with no revocation coverage."""
    from fastapi import HTTPException
    import pytest

    from hexgate_api.deps.tokens import optional_api_key

    pid = _signup_with_project(client, "optionaldep@example.com")
    minted = client.post(f"/v1/projects/{pid}/tokens", json={"name": "optional"})
    full, token_id = minted.json()["full"], minted.json()["id"]
    client.delete(f"/v1/projects/{pid}/tokens/{token_id}")

    async with session_factory() as session:
        with pytest.raises(HTTPException) as exc_info:
            await optional_api_key(f"Bearer {full}", session)

    assert exc_info.value.status_code == 401


async def test_find_token_by_secret_when_revoked_then_last_used_at_is_not_bumped(
    client: TestClient, session_factory
) -> None:
    """A revoked key must not keep updating its own activity timestamp — the
    dashboard's "last used" column would otherwise show a revoked key as live."""
    from hexgate_api.features.tokens.service import find_token_by_secret

    pid = _signup_with_project(client, "notbumped@example.com")
    minted = client.post(f"/v1/projects/{pid}/tokens", json={"name": "quiet"})
    full, token_id = minted.json()["full"], minted.json()["id"]
    client.get("/v1/me/key", headers={"Authorization": f"Bearer {full}"})
    client.delete(f"/v1/projects/{pid}/tokens/{token_id}")

    async with session_factory() as session:
        before = (await session.get(ApiKey, token_id)).last_used_at
        assert before is not None, "the pre-revoke introspection should have bumped it"

        assert await find_token_by_secret(session, full) is None

    async with session_factory() as session:
        assert (await session.get(ApiKey, token_id)).last_used_at == before


# ---------------------------------------------------------------------------
# mint_api_key() — service-level invariants
# ---------------------------------------------------------------------------


async def test_mint_api_key_happy_path(session_factory, tmp_path) -> None:
    """The token_id fact signed into the biscuit is the row's primary key.

    The OTLP Collector looks a token up in its cache by that fact, so a fact
    that doesn't match the row id points at nothing.
    """
    ks = FileKeyStore(base_dir=tmp_path / "keystore")
    ks.ensure_keypair()

    async with session_factory() as session:
        row, full_token = await mint_api_key(
            session,
            project_id=DEFAULT_PROJECT_ID,
            name="collector-key",
            scopes=["read_audit"],
            env="live",
            signing_key_bytes=ks._private_key_bytes(),
        )

    _, _, biscuit_b64 = parse_envelope(full_token)
    biscuit = verify_token(biscuit_b64, ks.public_key_bytes())

    minted = (
        AuthorizerBuilder().build(biscuit).query(Rule("found($id) <- token_id($id)"))
    )
    assert [f.terms[0] for f in minted] == [row.id]
