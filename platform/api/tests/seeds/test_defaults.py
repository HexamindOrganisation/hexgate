"""The HEXGATE_SEED_AGENTS knob (seeds/defaults.py).

``HEXGATE_SEED_AGENTS=skip`` keeps the triple-default (org/user/project) but skips
the sample agents — the knob the compose support-bot demo uses so its landed
project holds only the showcase agents (which it seeds itself in provision.py)
instead of the sample agents that would deny-all once the project turns compose.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import DEFAULT_PROJECT_ID
from hexgate_api.features.agents.service import get_agent
from hexgate_api.seeds.defaults import ensure_default_seed


@pytest_asyncio.fixture
async def factory():
    """A fresh in-memory DB with the schema but NO seed run yet."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def test_seed_agents_skip_keeps_project_but_drops_sample_agents(
    factory, monkeypatch
) -> None:
    monkeypatch.delenv("HEXGATE_SEED", raising=False)  # keep the triple-default on
    monkeypatch.setenv("HEXGATE_SEED_AGENTS", "skip")
    async with factory() as s:
        project = await ensure_default_seed(s)

    # The triple-default still lands (org/user/project) — only the agents are skipped.
    assert project is not None and project.id == DEFAULT_PROJECT_ID
    async with factory() as s:
        assert await get_agent(s, DEFAULT_PROJECT_ID, "default") is None
        assert await get_agent(s, DEFAULT_PROJECT_ID, "read_only") is None


async def test_seed_agents_default_seeds_the_sample_agents(
    factory, monkeypatch
) -> None:
    # Clear BOTH knobs so an ambient HEXGATE_SEED=skip (or HEXGATE_SEED_AGENTS) in
    # the environment can't flip the default path this test pins.
    monkeypatch.delenv("HEXGATE_SEED_AGENTS", raising=False)
    monkeypatch.delenv("HEXGATE_SEED", raising=False)
    async with factory() as s:
        await ensure_default_seed(s)
    async with factory() as s:
        assert await get_agent(s, DEFAULT_PROJECT_ID, "default") is not None
        assert await get_agent(s, DEFAULT_PROJECT_ID, "read_only") is not None
