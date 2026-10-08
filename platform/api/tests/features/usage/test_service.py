"""The usage query builder, the read mapping, the memo and the schema guard."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from hexgate_api.core.clickhouse import SchemaOutOfDate
from hexgate_api.features.usage import service
from hexgate_api.features.usage.paths import UsageMetric, UsageWindowSpec
from hexgate_api.features.usage.service import (
    _METRIC_SQL,
    _READ_COLUMNS,
    QUERY_SETTINGS,
    UsageMemo,
    UsageReadout,
    build_usage_query,
    read_usage,
)

_HOUR = UsageWindowSpec(UsageMetric.INVOCATIONS, 3_600)
_DAY_TOKENS = UsageWindowSpec(UsageMetric.TOTAL_TOKENS, 86_400)
_AS_OF = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _readout(value: int = 0) -> UsageReadout:
    return UsageReadout(as_of=_AS_OF, values={_HOUR: value})


def _key(name: str = "a") -> service.UsageMemoKey:
    return ("p", name, frozenset({_HOUR}))


# ---------------------------------------------------------------------------
# build_usage_query
# ---------------------------------------------------------------------------


def test_the_query_reads_each_spec_over_its_window_in_one_scan() -> None:
    sql, params = build_usage_query("p", "a", [_HOUR, _DAY_TOKENS])

    assert (
        "sumIf(invocations, minute >= toStartOfMinute(as_of - toIntervalSecond({w0:UInt32}))) AS v0"
        in sql
    )
    assert (
        "sumIf(input_tokens + output_tokens, "
        "minute >= toStartOfMinute(as_of - toIntervalSecond({w1:UInt32}))) AS v1"
    ) in sql
    assert "FROM usage_minute" in sql
    assert "GROUP BY" not in sql
    assert "FINAL" not in sql
    assert params == {
        "project_id": "p",
        "agent_name": "a",
        "w0": 3_600,
        "w1": 86_400,
        "scan": 86_400,
    }


def test_no_request_string_reaches_the_sql_text() -> None:
    agent_name, project_id = "x' OR 1=1 --", "p'"

    sql, params = build_usage_query(project_id, agent_name, [_HOUR])

    assert agent_name not in sql
    assert project_id not in sql
    assert params["agent_name"] == agent_name
    assert params["project_id"] == project_id


def test_specs_with_the_same_window_share_one_parameter() -> None:
    specs = [
        _HOUR,
        UsageWindowSpec(UsageMetric.TOOL_CALLS, 3_600),
        UsageWindowSpec(UsageMetric.DENIALS, 300),
    ]

    sql, params = build_usage_query("p", "a", specs)

    assert {k for k in params if k.startswith("w")} == {"w0", "w1"}
    assert params["scan"] == 3_600
    assert sql.count("{w0:UInt32}") == 2


def test_every_metric_has_its_sql() -> None:
    assert set(_METRIC_SQL) == set(UsageMetric)


# ---------------------------------------------------------------------------
# read_usage
# ---------------------------------------------------------------------------


def test_read_usage_maps_values_back_to_specs_by_position() -> None:
    client = MagicMock()
    client.query.return_value.result_rows = [[_AS_OF, 7, 1_234]]

    readout = read_usage(client, "p", "a", [_HOUR, _DAY_TOKENS])

    assert readout == UsageReadout(_AS_OF, {_HOUR: 7, _DAY_TOKENS: 1_234})
    assert client.query.call_args.kwargs["settings"] == QUERY_SETTINGS


def test_a_naive_as_of_is_read_as_utc() -> None:
    client = MagicMock()
    client.query.return_value.result_rows = [[_AS_OF.replace(tzinfo=None), 0]]

    assert read_usage(client, "p", "a", [_HOUR]).as_of == _AS_OF


# ---------------------------------------------------------------------------
# UsageMemo
# ---------------------------------------------------------------------------


async def test_a_hit_inside_the_ttl_does_not_load_again() -> None:
    clock = _FakeClock()
    memo = UsageMemo(1.0, 10, clock)
    loads: list[int] = []

    async def load() -> UsageReadout:
        loads.append(1)
        return _readout(len(loads))

    first = await memo.get_or_load(_key(), load)
    clock.now = 0.5
    second = await memo.get_or_load(_key(), load)
    clock.now = 1.0
    third = await memo.get_or_load(_key(), load)

    assert (first, second, third) == (_readout(1), _readout(1), _readout(2))


async def test_concurrent_misses_on_one_key_share_one_load() -> None:
    memo = UsageMemo(1.0, 10, _FakeClock())
    release = asyncio.Event()
    loads: list[int] = []

    async def load() -> UsageReadout:
        loads.append(1)
        await release.wait()
        return _readout(42)

    pending = asyncio.gather(*(memo.get_or_load(_key(), load) for _ in range(5)))
    await asyncio.sleep(0)
    release.set()

    assert await pending == [_readout(42)] * 5
    assert len(loads) == 1


async def test_a_failed_load_is_not_memoized() -> None:
    memo = UsageMemo(1.0, 10, _FakeClock())

    async def fail() -> UsageReadout:
        raise DatabaseError("boom")

    async def succeed() -> UsageReadout:
        return _readout(3)

    with pytest.raises(DatabaseError):
        await memo.get_or_load(_key(), fail)

    assert await memo.get_or_load(_key(), succeed) == _readout(3)


async def test_a_failure_does_not_evict_a_newer_entry_for_the_same_key() -> None:
    memo = UsageMemo(1.0, 1, _FakeClock())
    release_old = asyncio.Event()
    newer_loads: list[int] = []

    async def slow_fail() -> UsageReadout:
        await release_old.wait()
        raise DatabaseError("boom")

    async def newer() -> UsageReadout:
        newer_loads.append(1)
        return _readout(9)

    old = asyncio.ensure_future(memo.get_or_load(_key(), slow_fail))
    await asyncio.sleep(0)
    await memo.get_or_load(_key("other"), newer)  # full: evicts the pending "a"
    newer_loads.clear()
    assert await memo.get_or_load(_key(), newer) == _readout(9)
    release_old.set()
    with pytest.raises(DatabaseError):
        await old

    assert await memo.get_or_load(_key(), newer) == _readout(9)
    assert len(newer_loads) == 1


async def test_a_load_slower_than_the_ttl_is_still_shared() -> None:
    clock = _FakeClock()
    memo = UsageMemo(1.0, 10, clock)
    release = asyncio.Event()
    loads: list[int] = []

    async def slow() -> UsageReadout:
        loads.append(1)
        await release.wait()
        return _readout(4)

    first = asyncio.ensure_future(memo.get_or_load(_key(), slow))
    await asyncio.sleep(0)
    clock.now = 5.0
    second = asyncio.ensure_future(memo.get_or_load(_key(), slow))
    await asyncio.sleep(0)
    release.set()

    assert await first == await second == _readout(4)
    assert len(loads) == 1


async def test_the_ttl_runs_from_when_the_load_completes() -> None:
    clock = _FakeClock()
    memo = UsageMemo(1.0, 10, clock)
    release = asyncio.Event()
    loads: list[int] = []

    async def load() -> UsageReadout:
        loads.append(1)
        await release.wait()
        return _readout(len(loads))

    pending = asyncio.ensure_future(memo.get_or_load(_key(), load))
    await asyncio.sleep(0)
    clock.now = 5.0
    release.set()
    await pending

    clock.now = 5.5
    assert await memo.get_or_load(_key(), load) == _readout(1)
    clock.now = 6.0
    assert await memo.get_or_load(_key(), load) == _readout(2)


async def test_a_full_memo_keeps_a_pending_load_over_an_expired_one() -> None:
    clock = _FakeClock()
    memo = UsageMemo(1.0, 2, clock)
    release = asyncio.Event()
    loads: list[str] = []

    def loader(name: str, gate: asyncio.Event | None = None):
        async def load() -> UsageReadout:
            loads.append(name)
            if gate is not None:
                await gate.wait()
            return _readout()

        return load

    pending = asyncio.ensure_future(
        memo.get_or_load(_key("slow"), loader("slow", release))
    )
    await asyncio.sleep(0)
    await memo.get_or_load(_key("done"), loader("done"))
    clock.now = 5.0  # "done" expired; "slow" is still in flight
    await memo.get_or_load(_key("third"), loader("third"))
    joined = asyncio.ensure_future(memo.get_or_load(_key("slow"), loader("slow")))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(pending, joined)

    assert loads == ["slow", "done", "third"]


async def test_a_full_memo_drops_expired_entries_first_then_the_oldest() -> None:
    clock = _FakeClock()
    memo = UsageMemo(1.0, 2, clock)
    loads: list[str] = []

    def loader(name: str):
        async def load() -> UsageReadout:
            loads.append(name)
            return _readout()

        return load

    await memo.get_or_load(_key("old"), loader("old"))
    clock.now = 0.9
    await memo.get_or_load(_key("live"), loader("live"))
    clock.now = 1.0  # "old" expires, "live" doesn't
    await memo.get_or_load(_key("new"), loader("new"))
    await memo.get_or_load(_key("live"), loader("live"))
    assert loads == ["old", "live", "new"]

    await memo.get_or_load(_key("newest"), loader("newest"))  # full: evicts "live"
    await memo.get_or_load(_key("new"), loader("new"))
    await memo.get_or_load(_key("live"), loader("live"))
    assert loads == ["old", "live", "new", "newest", "live"]


# ---------------------------------------------------------------------------
# verify_schema
# ---------------------------------------------------------------------------


def _describing(columns: list[str]) -> MagicMock:
    client = MagicMock()
    result = MagicMock()
    result.column_names = ["name", "type"]
    result.result_rows = [[c, "UInt64"] for c in columns]
    client.query.return_value = result
    return client


def test_verify_schema_happy_path() -> None:
    service.verify_schema(_describing(list(_READ_COLUMNS)))  # no raise


def test_when_a_read_column_is_missing_then_verify_schema_names_it() -> None:
    columns = [c for c in _READ_COLUMNS if c != "input_tokens"]

    with pytest.raises(SchemaOutOfDate) as exc:
        service.verify_schema(_describing(columns))

    assert exc.value.missing == {"usage_minute": ["input_tokens"]}


def test_when_the_table_is_absent_then_verify_schema_reports_every_column() -> None:
    client = MagicMock()
    client.query.side_effect = DatabaseError(
        "Received ClickHouse exception, code: 60, server response: "
        "Code: 60. DB::Exception: Table hexgate_audit.usage_minute does not exist"
    )

    with pytest.raises(SchemaOutOfDate) as exc:
        service.verify_schema(client)

    assert exc.value.missing == {"usage_minute": sorted(_READ_COLUMNS)}
