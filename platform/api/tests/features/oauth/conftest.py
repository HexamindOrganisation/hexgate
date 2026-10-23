from __future__ import annotations

from collections.abc import Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import DEFAULT_PROJECT_ID, DEFAULT_USER_ID
from hexgate_api.core import keystore as keystore_mod
from hexgate_api.core.db import get_session
from hexgate_api.core.keystore import FileKeyStore
from hexgate_api.core.oauth_tokens import mint_access_token
from hexgate_api.main import app
from hexgate_api.seeds.defaults import ensure_default_project
from hexgate_api.settings import get_settings


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(session_factory):
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
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
def client(session_factory, oauth_keystore) -> Iterator[TestClient]:
    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def mint(**overrides) -> str:
    """An access token for the default user and project, shaped as the API mints it."""
    settings = get_settings()
    kwargs = {
        "sub": DEFAULT_USER_ID,
        "client_id": "https://client.example/meta.json",
        "scopes": ["policy:read"],
        "projects": [DEFAULT_PROJECT_ID],
        "issuer": settings.public_url,
        "audience": [settings.mcp_resource_url, settings.public_url],
    } | overrides
    return mint_access_token(**kwargs)
