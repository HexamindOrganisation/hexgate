"""Shared fixtures for the agents-slice tests.

``session_factory`` and ``client`` are declared once here so a new test module
inherits them. ``test_agents.py`` still carries its own module-level copies,
which shadow these and are behaviourally identical, so it can drop them
whenever it is next touched. (``test_bundle_signing.py`` has a differently
shaped ``session`` fixture and builds its clients inline; ``test_default_policy.py``
needs none of this.)
"""

from __future__ import annotations

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import DEFAULT_USER_ID
from hexgate_api.core import keystore as keystore_mod
from hexgate_api.core.db import get_session
from hexgate_api.main import app
from hexgate_api.seeds.defaults import ensure_default_project


@pytest_asyncio.fixture
async def session_factory():
    """Fresh in-memory async engine + factory, seeded with the triple-default.

    StaticPool so every session shares one connection — otherwise each
    ``:memory:`` handle gets its own private database.
    """
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
    """TestClient authenticated as the seeded default user.

    The identity dependency builds its session-JWT strategy from the platform
    signing key and the fixture doesn't run the app lifespan, so wire a
    throwaway keystore here and restore the original afterwards.
    """
    from hexgate_api.core.keystore import FileKeyStore

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    original_keystore = keystore_mod.keystore
    keystore_mod.keystore = FileKeyStore(base_dir=tmp_path / "keystore")
    keystore_mod.keystore.ensure_keypair()
    try:
        yield TestClient(app, headers={"X-Dev-User": DEFAULT_USER_ID})
    finally:
        app.dependency_overrides.clear()
        keystore_mod.keystore = original_keystore
