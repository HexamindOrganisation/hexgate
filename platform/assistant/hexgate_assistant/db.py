"""The engine and session factory for the assistant's own database."""

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hexgate_assistant.models import Base


def make_engine(database_url: URL) -> AsyncEngine:
    # pre_ping drops connections Postgres closed while idle.
    return create_async_engine(database_url, pool_pre_ping=True)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False keeps rows readable after a commit without a
    # reload, which on async would be an implicit (and failing) query.
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_tables(engine: AsyncEngine) -> None:
    """Create the tables that are missing; existing ones are left as they are
    (a column change needs a migration)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
