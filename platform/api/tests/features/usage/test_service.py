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
    METRIC_SQL,
    AGENT_ROLLUP,
    QUERY_SETTINGS,
    USER_ROLLUP,
    UsageMemo,
    UsageReadout,
    build_usage_query,
    read_usage,
)
from tests.features.usage.fakes import FakeClock

_HOUR = UsageWindowSpec(UsageMetric.INVOCATIONS, 3_600)
_DAY_TOKENS = UsageWindowSpec(UsageMetric.TOTAL_TOKENS, 86_400)
_AS_OF = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _readout(value: int = 0) -> UsageReadout:
    return UsageReadout(as_of=_AS_OF, values={_HOUR: value})


def _key(name: str = "a") -> service.UsageMemoKey:
    return ("p", name, (_HOUR,))


# ---------------------------------------------------------------------------
# build_usage_query
# ---------------------------------------------------------------------------


def test_the_query_reads_each_spec_over_its_window_in_one_scan() -> None:
    sql, params = build_usage_query(AGENT_ROLLUP, "p", "a", [_HOUR, _DAY_TOKENS])

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

    sql, params = build_usage_query(AGENT_ROLLUP, project_id, agent_name, [_HOUR])

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

    sql, params = build_usage_query(AGENT_ROLLUP, "p", "a", specs)

    assert {k for k in params if k.startswith("w")} == {"w0", "w1"}
    assert params["scan"] == 3_600
    assert sql.count("{w0:UInt32}") == 2


def test_the_agent_query_is_unchanged_by_the_rollup_parameter() -> None:
    sql, params = build_usage_query(AGENT_ROLLUP, "p", "a", [_HOUR, _DAY_TOKENS])

    assert sql == (
        "WITH now('UTC') AS as_of\n"
        "SELECT\n"
        "    as_of,\n"
        "    sumIf(invocations, minute >= toStartOfMinute(as_of - "
        "toIntervalSecond({w0:UInt32}))) AS v0,\n"
        "    sumIf(input_tokens + output_tokens, minute >= toStartOfMinute(as_of - "
        "toIntervalSecond({w1:UInt32}))) AS v1\n"
        "FROM usage_minute\n"
        "WHERE project_id = {project_id:String}\n"
        "  AND agent_name = {agent_name:String}\n"
        "  AND minute >= toStartOfMinute(as_of - toIntervalSecond({scan:UInt32}))"
    )
    assert params == {
        "project_id": "p",
        "agent_name": "a",
        "w0": 3_600,
        "w1": 86_400,
        "scan": 86_400,
    }


def test_the_user_query_reads_the_user_rollup_by_a_bound_user_id() -> None:
    user_id = "alice@x"

    sql, params = build_usage_query(USER_ROLLUP, "p", user_id, [_HOUR])

    assert "FROM usage_minute_user\n" in sql
    assert "  AND user_id = {user_id:String}\n" in sql
    assert "agent_name" not in sql
    assert user_id not in sql
    assert params["user_id"] == user_id
    assert "agent_name" not in params


def test_a_hostile_user_id_never_reaches_the_sql_text() -> None:
    user_id = "x' OR 1=1 --"

    sql, params = build_usage_query(USER_ROLLUP, "p", user_id, [_HOUR])

    assert user_id not in sql
    assert params["user_id"] == user_id


def test_each_rollup_reads_its_key_column() -> None:
    metrics = [
        "invocations",
        "tool_calls",
        "denials",
        "llm_calls",
        "input_tokens",
        "output_tokens",
    ]

    assert AGENT_ROLLUP.read_columns == ["project_id", "agent_name", "minute", *metrics]
    assert USER_ROLLUP.read_columns == ["project_id", "user_id", "minute", *metrics]


def test_every_metric_has_its_sql() -> None:
    assert set(METRIC_SQL) == set(UsageMetric)


# ---------------------------------------------------------------------------
# read_usage
# ---------------------------------------------------------------------------


def test_read_usage_maps_values_back_to_specs_by_position() -> None:
    client = MagicMock()
    client.query.return_value.result_rows = [[_AS_OF, 7, 1_234]]

    readout = read_usage(client, AGENT_ROLLUP, "p", "a", [_HOUR, _DAY_TOKENS])

    assert readout == UsageReadout(_AS_OF, {_HOUR: 7, _DAY_TOKENS: 1_234})
    assert client.query.call_args.kwargs["settings"] == QUERY_SETTINGS


def test_a_naive_as_of_is_read_as_utc() -> None:
    client = MagicMock()
    client.query.return_value.result_rows = [[_AS_OF.replace(tzinfo=None), 0]]

    assert read_usage(client, AGENT_ROLLUP, "p", "a", [_HOUR]).as_of == _AS_OF


# ---------------------------------------------------------------------------
# UsageMemo
# ---------------------------------------------------------------------------


async def test_a_hit_inside_the_ttl_does_not_load_again() -> None:
    clock = FakeClock()
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
    memo = UsageMemo(1.0, 10, FakeClock())
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
    memo = UsageMemo(1.0, 10, FakeClock())

    async def fail() -> UsageReadout:
        raise DatabaseError("boom")

    async def succeed() -> UsageReadout:
        return _readout(3)

    with pytest.raises(DatabaseError):
        await memo.get_or_load(_key(), fail)

    assert await memo.get_or_load(_key(), succeed) == _readout(3)


async def test_a_failure_does_not_evict_a_newer_entry_for_the_same_key() -> None:
    memo = UsageMemo(1.0, 10, FakeClock())
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
    stale = memo._entries[_key()][1]
    memo._entries[_key()] = (0.0, stale)  # replaced under it, as a reload would
    assert await memo.get_or_load(_key(), newer) == _readout(9)
    release_old.set()
    with pytest.raises(DatabaseError):
        await old

    assert await memo.get_or_load(_key(), newer) == _readout(9)
    assert len(newer_loads) == 1


async def test_a_load_slower_than_the_ttl_is_still_shared() -> None:
    clock = FakeClock()
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
    clock = FakeClock()
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
    clock = FakeClock()
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
    clock = FakeClock()
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


def _counting_loader(loads: list[str]):
    def loader(name: str, gate: asyncio.Event | None = None):
        async def load() -> UsageReadout:
            loads.append(name)
            if gate is not None:
                await gate.wait()
            return _readout()

        return load

    return loader


async def test_a_full_memo_never_evicts_a_pending_load() -> None:
    memo = UsageMemo(1.0, 2, FakeClock())
    release = asyncio.Event()
    loads: list[str] = []
    loader = _counting_loader(loads)

    pending = asyncio.ensure_future(
        memo.get_or_load(_key("slow"), loader("slow", release))
    )
    await asyncio.sleep(0)
    await memo.get_or_load(_key("live"), loader("live"))
    await memo.get_or_load(_key("third"), loader("third"))  # full: evicts "live"
    joined = asyncio.ensure_future(memo.get_or_load(_key("slow"), loader("slow")))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(pending, joined)

    assert loads == ["slow", "live", "third"]


async def test_a_memo_full_of_pending_loads_overflows_rather_than_drops_one() -> None:
    memo = UsageMemo(1.0, 1, FakeClock())
    release = asyncio.Event()
    loads: list[str] = []
    loader = _counting_loader(loads)

    first = asyncio.ensure_future(memo.get_or_load(_key("a"), loader("a", release)))
    second = asyncio.ensure_future(memo.get_or_load(_key("b"), loader("b", release)))
    await asyncio.sleep(0)
    joins = [
        asyncio.ensure_future(memo.get_or_load(_key(name), loader(name)))
        for name in ("a", "b")
    ]
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second, *joins)

    assert loads == ["a", "b"]


async def test_reloading_an_expired_key_does_not_evict_a_live_one() -> None:
    clock = FakeClock()
    memo = UsageMemo(1.0, 2, clock)
    loads: list[str] = []
    loader = _counting_loader(loads)

    await memo.get_or_load(_key("expired"), loader("expired"))
    clock.now = 0.5
    await memo.get_or_load(_key("live"), loader("live"))
    clock.now = 1.0  # "expired" expires, "live" doesn't
    await memo.get_or_load(_key("expired"), loader("expired"))
    await memo.get_or_load(_key("live"), loader("live"))

    assert loads == ["expired", "live", "expired"]


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


_BOTH_ROLLUPS_COLUMNS = sorted(
    set(AGENT_ROLLUP.read_columns) | set(USER_ROLLUP.read_columns)
)


def test_verify_schema_happy_path() -> None:
    service.verify_schema(_describing(_BOTH_ROLLUPS_COLUMNS))  # no raise


def test_when_a_read_column_is_missing_then_verify_schema_names_it() -> None:
    columns = [c for c in _BOTH_ROLLUPS_COLUMNS if c != "input_tokens"]

    with pytest.raises(SchemaOutOfDate) as exc:
        service.verify_schema(_describing(columns))

    assert exc.value.missing == {
        "usage_minute": ["input_tokens"],
        "usage_minute_user": ["input_tokens"],
    }


def test_when_the_user_key_column_is_missing_then_verify_schema_names_it() -> None:
    columns = [c for c in _BOTH_ROLLUPS_COLUMNS if c != "user_id"]

    with pytest.raises(SchemaOutOfDate) as exc:
        service.verify_schema(_describing(columns))

    assert exc.value.missing == {"usage_minute_user": ["user_id"]}


def test_when_the_table_is_absent_then_verify_schema_reports_every_column() -> None:
    client = MagicMock()
    client.query.side_effect = DatabaseError(
        "Received ClickHouse exception, code: 60, server response: "
        "Code: 60. DB::Exception: Table hexgate_audit.usage_minute does not exist"
    )

    with pytest.raises(SchemaOutOfDate) as exc:
        service.verify_schema(client)

    assert exc.value.missing == {
        "usage_minute": sorted(AGENT_ROLLUP.read_columns),
        "usage_minute_user": sorted(USER_ROLLUP.read_columns),
    }


def test_when_only_the_user_rollup_is_absent_then_only_it_is_reported() -> None:
    """The boot gate: an API on a volume without migration 0006 refuses to start."""
    described = _describing(AGENT_ROLLUP.read_columns).query.return_value

    def query(sql: str) -> MagicMock:
        if "usage_minute_user" in sql:
            raise DatabaseError(
                "Received ClickHouse exception, code: 60, server response: Code: 60. "
                "DB::Exception: Table hexgate_audit.usage_minute_user does not exist"
            )
        return described

    client = MagicMock()
    client.query.side_effect = query

    with pytest.raises(SchemaOutOfDate) as exc:
        service.verify_schema(client)

    assert exc.value.missing == {"usage_minute_user": sorted(USER_ROLLUP.read_columns)}


def test_user_reads_get_their_own_pool_and_memo() -> None:
    """A burst of cold users must not queue or evict agent reads."""
    assert service.get_user_usage_executor() is not service.get_usage_executor()
    assert service.get_user_usage_memo() is not service.get_usage_memo()
