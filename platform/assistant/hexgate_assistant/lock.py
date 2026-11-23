"""One turn at a time per user, across sessions, projects and orgs.

A turn holds the user's ``assistant_turn_lock`` row while it runs. A row left
behind by a turn that crashed before its release is taken over once it is
older than :data:`STALE_AFTER`, so a crash never locks a user out for good.
"""

import asyncio
import contextlib
import functools
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import anyio
from sqlalchemy import delete
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hexgate_assistant.models import AssistantTurnLock, utcnow

logger = logging.getLogger(__name__)

# How long a turn may run, counted from the `started_at` that `turn_lock`
# yields; the turn runner enforces it.
TURN_DEADLINE = timedelta(minutes=5)
# A live turn ends, its store included, within the deadline; the minute on
# top keeps that store clear of a takeover. An older row is a crashed turn's.
STALE_AFTER = TURN_DEADLINE + timedelta(minutes=1)
# A release that cannot reach the database within this gives up waiting; the
# row then goes stale.
RELEASE_SECONDS = 10

_INSERTS = {"postgresql": postgresql.insert, "sqlite": sqlite.insert}


class TurnBusy(Exception):
    """Another turn of the same user holds the lock."""


async def take(
    db: async_sessionmaker[AsyncSession], user_id: str, turn_id: str
) -> datetime:
    """Hold the user's lock for `turn_id` and return its `started_at`, or
    raise :class:`TurnBusy`.

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
    return now


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


@contextlib.asynccontextmanager
async def turn_lock(
    db: async_sessionmaker[AsyncSession], user_id: str
) -> AsyncIterator[tuple[str, datetime]]:
    """Hold the user's lock for the block, under a new turn id; yields
    `(turn_id, started_at)`. Raises :class:`TurnBusy` before entering it
    when another turn holds the lock."""
    turn_id = str(uuid.uuid4())
    try:
        started_at = await take(db, user_id, turn_id)
    except asyncio.CancelledError:
        # Cancelled mid-take: the insert may have committed. A busy or
        # failed take left no row of this turn.
        await _release_for_good(db, user_id, turn_id)
        raise
    try:
        yield turn_id, started_at
    finally:
        await _release_for_good(db, user_id, turn_id)


# Releases still running after their turn stopped waiting; held so the event
# loop does not drop the task before it ends.
_releases: set[asyncio.Task[None]] = set()


async def _release_for_good(
    db: async_sessionmaker[AsyncSession], user_id: str, turn_id: str
) -> None:
    """Release in a task of its own, which neither cancellation reaches.

    Under an anyio cancel scope (Starlette's, on a client disconnect) every
    await of a cancelled task raises again: the shielded scope keeps it off
    this wait. A second native ``task.cancel()`` does reach this wait, and
    `asyncio.shield` keeps it off the release, which runs to the end. Either
    way the caller stops waiting after :data:`RELEASE_SECONDS`. A failed
    release is logged, never raised.
    """
    task = asyncio.create_task(release(db, user_id, turn_id))
    _releases.add(task)
    task.add_done_callback(functools.partial(_released, turn_id))
    with anyio.move_on_after(RELEASE_SECONDS, shield=True) as wait:
        # The outcome is logged by `_released`; the turn's own error, if
        # any, is the one that propagates.
        with contextlib.suppress(Exception):
            await asyncio.shield(task)
    if wait.cancelled_caught:
        logger.warning("turn %s: lock release still running", turn_id)


def _released(turn_id: str, task: asyncio.Task[None]) -> None:
    _releases.discard(task)
    if not task.cancelled() and task.exception() is not None:
        # The row stays until it goes stale.
        logger.error("turn %s: lock release failed", turn_id, exc_info=task.exception())
