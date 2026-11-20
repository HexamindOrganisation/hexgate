"""The policy-change record and the org's assistant mode at the database."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import DEFAULT_ORG_ID, DEFAULT_PROJECT_ID
from hexgate_api.core.ids import new_id
from hexgate_api.models import AssistantChange, Organization
from hexgate_api.seeds.defaults import ensure_default_project


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as bootstrap:
        await ensure_default_project(bootstrap)
    yield maker
    await engine.dispose()


async def test_assistant_change_happy_path(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    files = [
        {
            "name": "policy.yaml",
            "before_content": "a: 1\n",
            "before_hash": "b" * 64,
            "after_content": "a: 2\n",
            "after_hash": "c" * 64,
        }
    ]
    change = AssistantChange(
        id=new_id(AssistantChange),
        project_id=DEFAULT_PROJECT_ID,
        summary="Lower the refund cap",
        files=files,
        samples=[{"role": "billing", "tool": "refund_order", "expect": "deny"}],
        checks=[{"id": "yaml", "status": "pass", "detail": "", "items": []}],
        boundary_changes=[],
        effect={
            "changes": [],
            "agent_changes": [],
            "gate_changes": [],
            "agents_affected": [],
            "composes": {"before": True, "after": True},
        },
    )
    async with factory() as session:
        session.add(change)
        await session.commit()

    async with factory() as session:
        stored = await session.get(AssistantChange, change.id)

    assert stored is not None
    assert stored.id.startswith("chg_")
    assert stored.files == files
    assert stored.checks[0]["status"] == "pass"
    assert (stored.session_id, stored.client_id, stored.created_by_user_id) == (
        None,
        None,
        None,
    )


async def test_when_an_old_build_inserts_an_org_then_it_reads_read_write(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A build that predates the column omits it from its INSERT.

    The column's server default fills it, so that build keeps creating orgs
    between the migration and the deploy of the code that reads the column.
    """
    async with factory() as session:
        await session.exec(
            text(
                "INSERT INTO organization (id, slug, name, created_at, updated_at) "
                "VALUES ('org-old', 'old', 'Old', '2026-11-17', '2026-11-17')"
            )
        )
        await session.commit()

    async with factory() as session:
        org = await session.get(Organization, "org-old")
        seeded = await session.get(Organization, DEFAULT_ORG_ID)

    assert org is not None and org.assistant_mode == "read_write"
    assert seeded is not None and seeded.assistant_mode == "read_write"
