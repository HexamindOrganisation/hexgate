import asyncio
import uuid
from datetime import timedelta

import anyio
import pytest
from sqlalchemy import select

from hexgate_assistant import lock
from hexgate_assistant.lock import STALE_AFTER, TurnBusy, release, take, turn_lock
from hexgate_assistant.models import AssistantTurnLock, utcnow


async def _holder(db, user_id: str = "u") -> str | None:
    async with db() as s:
        return await s.scalar(
            select(AssistantTurnLock.turn_id).where(
                AssistantTurnLock.user_id == user_id
            )
        )


async def _held_since(db, age: timedelta, turn_id: str = "old") -> None:
    async with db() as s:
        s.add(
            AssistantTurnLock(user_id="u", turn_id=turn_id, started_at=utcnow() - age)
        )
        await s.commit()


async def test_take_happy_path(db):
    await take(db, "u", "t1")

    assert await _holder(db) == "t1"


@pytest.mark.parametrize(
    ("age", "holder"),
    [
        (STALE_AFTER - timedelta(seconds=10), "old"),
        (STALE_AFTER + timedelta(seconds=1), "new"),
    ],
    ids=["fresh lock: busy", "stale lock: taken over"],
)
async def test_when_lock_is_held_then_only_a_stale_one_is_taken(db, age, holder):
    await _held_since(db, age)

    if holder == "old":
        with pytest.raises(TurnBusy):
            await take(db, "u", "new")
    else:
        await take(db, "u", "new")

    assert await _holder(db) == holder


async def test_when_another_user_holds_a_lock_then_take_succeeds(db):
    await take(db, "other", "t1")

    await take(db, "u", "t2")

    assert await _holder(db) == "t2"


async def test_when_a_taken_over_turn_releases_then_successor_keeps_the_lock(db):
    await _held_since(db, STALE_AFTER + timedelta(seconds=1))
    await take(db, "u", "t2")

    await release(db, "u", "old")

    assert await _holder(db) == "t2"


async def test_turn_lock_happy_path(db):
    before = utcnow()
    async with turn_lock(db, "u") as (turn_id, started_at):
        assert await _holder(db) == turn_id
        assert uuid.UUID(turn_id).version == 4
        assert started_at >= before

    assert await _holder(db) is None


async def test_when_the_turn_raises_then_the_lock_is_released(db):
    with pytest.raises(RuntimeError):
        async with turn_lock(db, "u"):
            raise RuntimeError("turn failed")

    assert await _holder(db) is None


async def test_when_busy_then_turn_lock_releases_nothing(db, monkeypatch):
    await take(db, "u", "t1")
    released = []

    async def recorded_release(*args):
        released.append(args)

    monkeypatch.setattr(lock, "release", recorded_release)

    with pytest.raises(TurnBusy):
        async with turn_lock(db, "u"):
            pytest.fail("the block ran without the lock")

    assert released == []


async def test_when_release_fails_then_the_turns_own_error_propagates(
    db, monkeypatch, caplog
):
    async def broken_release(*_):
        raise ConnectionError("database gone")

    monkeypatch.setattr(lock, "release", broken_release)

    with pytest.raises(RuntimeError, match="turn failed"):
        async with turn_lock(db, "u"):
            raise RuntimeError("turn failed")

    assert "lock release failed" in caplog.text


@pytest.mark.parametrize("stale", [False, True], ids=["free", "stale"])
async def test_when_two_turns_race_for_the_lock_then_one_wins(db, stale):
    if stale:
        await _held_since(db, STALE_AFTER + timedelta(seconds=1))

    results = await asyncio.gather(
        take(db, "u", "t1"), take(db, "u", "t2"), return_exceptions=True
    )

    busy = [isinstance(r, TurnBusy) for r in results]
    assert sorted(busy) == [False, True]
    assert await _holder(db) == ("t2" if busy[0] else "t1")


async def test_when_the_turn_is_cancelled_then_the_lock_is_released(db):
    # Starlette cancels a streamed response's task group on a disconnect;
    # anyio then cancels every later await in the task, the release's too.
    async def turn(task_status=anyio.TASK_STATUS_IGNORED):
        async with turn_lock(db, "u"):
            task_status.started()
            await anyio.sleep_forever()

    async with anyio.create_task_group() as tg:
        await tg.start(turn)
        tg.cancel_scope.cancel()

    assert await _holder(db) is None


async def test_when_the_turn_is_cancelled_twice_then_the_lock_is_released(db):
    # A native cancel, then a second one landing while the release runs.
    entered = asyncio.Event()

    async def turn():
        async with turn_lock(db, "u"):
            entered.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(turn())
    with anyio.fail_after(5):
        await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert lock._releases, "the second cancel must land during the release"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(5):
            await task

    with anyio.fail_after(5):
        while await _holder(db) is not None:
            await asyncio.sleep(0.01)


async def test_when_release_hangs_then_the_turn_returns_and_the_lock_frees_later(
    db, monkeypatch
):
    database_back = asyncio.Event()

    async def slow_release(*args):
        await database_back.wait()
        await release(*args)

    monkeypatch.setattr(lock, "release", slow_release)
    monkeypatch.setattr(lock, "RELEASE_SECONDS", 0.05)

    with anyio.fail_after(5):
        async with turn_lock(db, "u"):
            pass
    assert await _holder(db) is not None

    database_back.set()
    with anyio.fail_after(5):
        while await _holder(db) is not None:
            await asyncio.sleep(0.01)
