"""Shared fixtures for the policy_modules tests."""

from __future__ import annotations

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core import keystore as keystore_mod
from hexgate_api.main import app
from hexgate_api.seeds.defaults import ensure_default_project


@pytest_asyncio.fixture
async def session_factory():
    """In-memory async SQLite + factory, seeded with the triple-default."""
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


def _project(client: TestClient) -> str:
    """Sign up (=> org owner), create a project, return its id."""
    client.post(
        "/v1/auth/register",
        json={"email": "pol@example.com", "password": "correcthorsebattery"},
    )
    client.post(
        "/v1/auth/cookie/login",
        data={"username": "pol@example.com", "password": "correcthorsebattery"},
    )
    org_id = client.get("/v1/orgs").json()[0]["id"]
    r = client.post(f"/v1/orgs/{org_id}/projects", json={"name": "modular"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _put_module(client, pid, tier, path, content) -> None:
    r = client.put(
        f"/v1/projects/{pid}/policy-modules/{tier}/{path}", json={"content": content}
    )
    assert r.status_code == 200, r.text


def _put_file(client, pid, name, content):
    return client.put(
        f"/v1/projects/{pid}/policy-files/{name}", json={"content": content}
    )
