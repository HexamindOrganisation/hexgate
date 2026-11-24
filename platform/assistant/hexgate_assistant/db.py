"""The engine and session factory for the assistant's own database."""

from sqlalchemy import URL, Connection, inspect
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


class SchemaOutOfDate(RuntimeError):
    """Model columns the live tables lack, as {table: [columns]}."""

    def __init__(self, missing: dict[str, list[str]]) -> None:
        self.missing = missing
        gaps = ", ".join(f"{t}.{c}" for t, cols in missing.items() for c in cols)
        super().__init__(
            f"the hexgate_assistant database lacks columns the code needs: {gaps} "
            "(create_all never alters an existing table; apply the migration "
            "that adds them)"
        )


async def verify_schema(engine: AsyncEngine) -> None:
    """Raise :class:`SchemaOutOfDate` naming every model column missing from
    its live table, so an older database stops the start-up instead of
    failing the first query that reads the column. Extra live columns are
    allowed (an older build running after a newer migration). Types and
    nullability are not compared."""
    async with engine.connect() as conn:
        missing = await conn.run_sync(_missing_columns)
    if missing:
        raise SchemaOutOfDate(missing)


def _missing_columns(conn: Connection) -> dict[str, list[str]]:
    inspector = inspect(conn)
    missing = {}
    for table in Base.metadata.sorted_tables:
        live = {column["name"] for column in inspector.get_columns(table.name)}
        gaps = sorted(
            column.name for column in table.columns if column.name not in live
        )
        if gaps:
            missing[table.name] = gaps
    return missing
