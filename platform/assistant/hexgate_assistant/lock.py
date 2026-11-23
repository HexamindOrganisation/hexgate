"""One turn at a time per user, across sessions, projects and orgs.

A turn holds the user's ``assistant_turn_lock`` row while it runs. A row left
behind by a turn that crashed before its release is taken over once it is
older than :data:`STALE_AFTER`, so a crash never locks a user out for good.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import anyio
from sqlalchemy import delete
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hexgate_assistant.models import AssistantTurnLock, utcnow

# A turn ends within 5 minutes, so an older row is a crashed turn's.
STALE_AFTER = timedelta(minutes=5)

_INSERTS = {"postgresql": postgresql.insert, "sqlite": sqlite.insert}


class TurnBusy(Exception):
    """Another turn of the same user holds the lock."""


async def take(
    db: async_sessionmaker[AsyncSession], user_id: str, turn_id: str
) -> None:
    """Hold the user's lock for `turn_id`, or raise :class:`TurnBusy`.

    One statement, so two takers racing for a free or stale lock cannot
    both win: the insert, or on conflict the takeover of a stale row, and
    nothing when the row is fresh.
    """
    now = utcnow()
    async with db() as session:
        table = AssistantTurnLock.__table__
        insert = _INSERTS[session.bind.dialect.name](table)
        statement = (
            insert.values(user_id=user_id, turn_id=turn_id, started_at=now)
            .on_conflict_do_update(
                index_elements=[table.c.user_id],
                set_={"turn_id": turn_id, "started_at": now},
                where=table.c.started_at < now - STALE_AFTER,
            )
            .returning(table.c.turn_id)
        )
        taken = (await session.execute(statement)).scalar_one_or_none()
        await session.commit()
    if taken is None:
        raise TurnBusy(user_id)


async def release(
    db: async_sessionmaker[AsyncSession], user_id: str, turn_id: str
) -> None:
    """Drop the lock if `turn_id` still holds it: a turn whose lock was taken
    over as stale must not release its successor's."""
    async with db() as session:
        await session.execute(
            delete(AssistantTurnLock).where(
                AssistantTurnLock.user_id == user_id,
                AssistantTurnLock.turn_id == turn_id,
            )
        )
        await session.commit()


@asynccontextmanager
async def turn_lock(
    db: async_sessionmaker[AsyncSession], user_id: str, turn_id: str
) -> AsyncIterator[None]:
    """Hold the user's lock for the block; raises :class:`TurnBusy` before
    entering it when another turn holds it."""
    try:
        # Inside the try: a cancel landing after the insert committed still
        # releases. A busy take releases nothing, the row naming another turn.
        await take(db, user_id, turn_id)
        yield
    finally:
        # Shielded: under an anyio cancel scope (Starlette's, on a client
        # disconnect) every await of a cancelled task raises again, so an
        # unshielded release never reaches the database and the user stays
        # locked out until the row goes stale.
        with anyio.CancelScope(shield=True):
            await release(db, user_id, turn_id)
