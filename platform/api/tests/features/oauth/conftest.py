from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core import keystore as keystore_mod
from hexgate_api.core.db import get_session
from hexgate_api.core.keystore import FileKeyStore
from hexgate_api.features.oauth import client_cache
from hexgate_api.features.oauth.client_metadata import ClientMetadataError
from hexgate_api.main import create_app
from hexgate_api.models import Organization, OrganizationMember, Project, User
from hexgate_api.seeds.defaults import ensure_default_project
from hexgate_api.settings import get_settings
from tests.features.oauth import consent_flow


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
async def session(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        await ensure_default_project(session)
        yield session


@pytest_asyncio.fixture
async def consent_client(engine, tmp_path, monkeypatch: pytest.MonkeyPatch):
    """The real app on the test engine, logged in through ``X-Dev-User``.

    Seeded with the default org (the user's) and an org the user is not in,
    whose only member is someone else.
    """
    for name, value in consent_flow.ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    keys = FileKeyStore(base_dir=tmp_path / "keystore")
    keys.ensure_keypair()
    monkeypatch.setattr(keystore_mod, "keystore", keys)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as seed:
        await ensure_default_project(seed)
        seed.add(Organization(id=consent_flow.OTHER_ORG_ID, slug="other", name="Other"))
        seed.add(
            Project(
                id=consent_flow.OTHER_PROJECT_ID,
                org_id=consent_flow.OTHER_ORG_ID,
                name="elsewhere",
            )
        )
        seed.add(User(id=consent_flow.OTHER_USER_ID, email="other@example.test"))
        seed.add(
            OrganizationMember(
                user_id=consent_flow.OTHER_USER_ID,
                org_id=consent_flow.OTHER_ORG_ID,
                role="owner",
            )
        )
        await seed.commit()

    async def fake_fetch(client_id, *, allow_insecure_localhost=False):
        if client_id != consent_flow.CLIENT_ID:
            raise ClientMetadataError("client_id answered HTTP 404")
        return consent_flow.DOCUMENT

    monkeypatch.setattr(client_cache, "fetch_client_metadata", fake_fetch)
    app = create_app()

    async def override_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()
