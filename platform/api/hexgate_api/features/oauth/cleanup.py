"""Purge dead OAuth rows in the background, off the token path.

One task per process, started and stopped by the app's lifespan. A failed run
is logged and the next one tries again: a purge never fails a login.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from hexgate_api.core import db
from hexgate_api.features.oauth import grants

FIRST_RUN_DELAY_SECONDS = 60
INTERVAL_SECONDS = 600

logger = logging.getLogger(__name__)


async def purge_once() -> None:
    try:
        async with db.async_session_factory() as session:
            await grants.purge_dead_rows(session)
    except Exception:
        logger.warning("oauth purge failed; retrying next run", exc_info=True)


async def purge_periodically(
    first_delay: float = FIRST_RUN_DELAY_SECONDS, interval: float = INTERVAL_SECONDS
) -> None:
    await asyncio.sleep(first_delay)
    while True:
        await purge_once()
        await asyncio.sleep(interval)


@asynccontextmanager
async def purging() -> AsyncIterator[None]:
    """Run ``purge_periodically`` for the duration of the block."""
    task = asyncio.create_task(purge_periodically())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
