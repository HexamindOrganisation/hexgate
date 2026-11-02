"""read_usage against a real local ClickHouse (needs migrations 0005, 0006).

Rows go straight into the rollups: they're plain tables, and no view fires on an
insert into them. Opt-in via `pytest -m integration`.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from hexgate_api.features.usage.paths import UsageMetric, UsageWindowSpec
from hexgate_api.features.usage.service import (
    AGENT_ROLLUP,
    USAGE_MINUTE_TABLE,
    USAGE_MINUTE_USER_TABLE,
    USER_ROLLUP,
    read_usage,
)
from hexgate_api.features.usage.top_users import (
    TOP_USERS_WINDOW_SECONDS,
    read_top_users,
)

_COLUMNS = [
    "project_id",
    "agent_name",
    "minute",
    "invocations",
    "input_tokens",
    "output_tokens",
]
_USER_COLUMNS = ["user_id" if c == "agent_name" else c for c in _COLUMNS]
_AS_OF_TOLERANCE = timedelta(seconds=30)
_SECONDS_PER_MINUTE = 60
# Past this second, wait for the next minute so the read can't cross into it.
_LATEST_SAFE_SECOND = 55


def _start_of_minute(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


def _minute(ago: timedelta) -> datetime:
    return _start_of_minute(datetime.now(UTC) - ago)


def _clickhouse_now(clickhouse) -> datetime:
    now = clickhouse.query("SELECT now('UTC')").result_rows[0][0]
    return now if now.tzinfo is not None else now.replace(tzinfo=UTC)


def _clickhouse_now_early_in_a_minute(clickhouse) -> datetime:
    now = _clickhouse_now(clickhouse)
    if now.second < _LATEST_SAFE_SECOND:
        return now
    time.sleep(_SECONDS_PER_MINUTE - now.second)
    return _clickhouse_now(clickhouse)


@pytest.fixture
def clickhouse():
    from hexgate_api.core.clickhouse import get_clickhouse

    return get_clickhouse()


@pytest.fixture
def project_id(clickhouse):
    pid = f"test_proj_{uuid.uuid4().hex[:8]}"
    try:
        yield pid
    finally:
        for table in (USAGE_MINUTE_TABLE, USAGE_MINUTE_USER_TABLE):
            clickhouse.command(
                f"ALTER TABLE {table} "
                "DELETE WHERE startsWith(project_id, {pid:String})",
                parameters={"pid": pid},
            )


@pytest.mark.integration
def test_windows_sum_the_agent_rows_inside_them(clickhouse, project_id: str) -> None:
    # Shares the fixture's prefix, so its cleanup covers this project too.
    other_project = f"{project_id}_other"
    rows = [
        [project_id, "a", _minute(timedelta(minutes=2)), 1, 10, 20],
        # Same key as the row above: summed, never assumed merged.
        [project_id, "a", _minute(timedelta(minutes=2)), 2, 0, 0],
        [project_id, "a", _minute(timedelta(minutes=30)), 4, 100, 200],
        [project_id, "a", _minute(timedelta(hours=3)), 8, 0, 0],
        [project_id, "a", _minute(timedelta(days=2)), 16, 0, 0],
        [project_id, "b", _minute(timedelta(minutes=2)), 32, 0, 0],
        [other_project, "a", _minute(timedelta(minutes=2)), 64, 0, 0],
    ]
    clickhouse.insert(USAGE_MINUTE_TABLE, rows, column_names=_COLUMNS)
    specs = [
        UsageWindowSpec(UsageMetric.INVOCATIONS, 300),
        UsageWindowSpec(UsageMetric.INVOCATIONS, 3_600),
        UsageWindowSpec(UsageMetric.INVOCATIONS, 86_400),
        UsageWindowSpec(UsageMetric.INVOCATIONS, 7 * 86_400),
        UsageWindowSpec(UsageMetric.TOTAL_TOKENS, 86_400),
    ]
    readout = read_usage(clickhouse, AGENT_ROLLUP, project_id, "a", specs)

    assert [readout.values[s] for s in specs] == [3, 7, 15, 31, 330]
    assert readout.as_of.tzinfo is not None
    assert abs(readout.as_of - datetime.now(UTC)) < _AS_OF_TOLERANCE


@pytest.mark.integration
def test_an_agent_with_no_rows_reads_zeros(clickhouse, project_id: str) -> None:
    specs = [
        UsageWindowSpec(UsageMetric.TOOL_CALLS, 3_600),
        UsageWindowSpec(UsageMetric.LLM_CALLS, 300),
    ]

    readout = read_usage(clickhouse, AGENT_ROLLUP, project_id, "nobody", specs)

    assert dict(readout.values) == {spec: 0 for spec in specs}


@pytest.mark.integration
def test_the_leading_partial_minute_is_in_the_window(
    clickhouse, project_id: str
) -> None:
    # The window starts from ClickHouse's now(), so the row's minute must too.
    window = timedelta(minutes=5)
    first_minute = _start_of_minute(
        _clickhouse_now_early_in_a_minute(clickhouse) - window
    )
    clickhouse.insert(
        USAGE_MINUTE_TABLE,
        [[project_id, "a", first_minute, 1, 0, 0]],
        column_names=_COLUMNS,
    )

    readout = read_usage(
        clickhouse,
        AGENT_ROLLUP,
        project_id,
        "a",
        [UsageWindowSpec(UsageMetric.INVOCATIONS, int(window.total_seconds()))],
    )

    assert _start_of_minute(readout.as_of - window) == first_minute
    assert list(readout.values.values()) == [1]


@pytest.mark.integration
def test_user_windows_sum_only_that_users_rows(clickhouse, project_id: str) -> None:
    other_project = f"{project_id}_other"
    rows = [
        [project_id, "alice", _minute(timedelta(minutes=2)), 1, 10, 20],
        [project_id, "alice", _minute(timedelta(minutes=2)), 2, 0, 0],
        [project_id, "alice", _minute(timedelta(minutes=30)), 4, 100, 200],
        [project_id, "alice", _minute(timedelta(hours=3)), 8, 0, 0],
        [project_id, "alice", _minute(timedelta(days=2)), 16, 0, 0],
        [project_id, "bob", _minute(timedelta(minutes=2)), 32, 0, 0],
        [other_project, "alice", _minute(timedelta(minutes=2)), 64, 0, 0],
    ]
    clickhouse.insert(USAGE_MINUTE_USER_TABLE, rows, column_names=_USER_COLUMNS)
    specs = [
        UsageWindowSpec(UsageMetric.INVOCATIONS, 300),
        UsageWindowSpec(UsageMetric.INVOCATIONS, 3_600),
        UsageWindowSpec(UsageMetric.INVOCATIONS, 86_400),
        UsageWindowSpec(UsageMetric.INVOCATIONS, 7 * 86_400),
        UsageWindowSpec(UsageMetric.TOTAL_TOKENS, 86_400),
    ]

    readout = read_usage(clickhouse, USER_ROLLUP, project_id, "alice", specs)

    assert [readout.values[s] for s in specs] == [3, 7, 15, 31, 330]


@pytest.mark.integration
def test_a_user_with_no_rows_reads_zeros(clickhouse, project_id: str) -> None:
    specs = [
        UsageWindowSpec(UsageMetric.TOOL_CALLS, 3_600),
        UsageWindowSpec(UsageMetric.LLM_CALLS, 300),
    ]

    readout = read_usage(clickhouse, USER_ROLLUP, project_id, "nobody", specs)

    assert dict(readout.values) == {spec: 0 for spec in specs}


def _ranking(
    clickhouse, project_id: str, window: str, metric: UsageMetric, limit: int = 10
) -> list[tuple[str, int]]:
    users = read_top_users(
        clickhouse, project_id, TOP_USERS_WINDOW_SECONDS[window], metric, limit
    )
    return [(u.user_id, u.value) for u in users]


@pytest.mark.integration
def test_top_users_rank_one_project_within_the_window(
    clickhouse, project_id: str
) -> None:
    other_project = f"{project_id}_other"
    rows = [
        [project_id, "alice", _minute(timedelta(minutes=2)), 10, 0, 0],
        # Same user, another minute and another insert: summed into one entry.
        [project_id, "bob", _minute(timedelta(hours=1)), 50, 0, 0],
        [project_id, "carol", _minute(timedelta(days=2)), 100, 0, 0],
        [other_project, "dave", _minute(timedelta(minutes=2)), 999, 0, 0],
    ]
    clickhouse.insert(USAGE_MINUTE_USER_TABLE, rows, column_names=_USER_COLUMNS)
    clickhouse.insert(
        USAGE_MINUTE_USER_TABLE,
        [
            [project_id, "alice", _minute(timedelta(minutes=2)), 5, 0, 0],
            [project_id, "alice", _minute(timedelta(hours=5)), 15, 0, 0],
        ],
        column_names=_USER_COLUMNS,
    )

    day = _ranking(clickhouse, project_id, "24h", UsageMetric.INVOCATIONS)
    week = _ranking(clickhouse, project_id, "7d", UsageMetric.INVOCATIONS)

    assert day == [("bob", 50), ("alice", 30)]
    assert week == [("carol", 100), ("bob", 50), ("alice", 30)]
    assert all(user for user, _ in week)


@pytest.mark.integration
def test_top_users_leave_out_users_at_zero(clickhouse, project_id: str) -> None:
    clickhouse.insert(
        USAGE_MINUTE_USER_TABLE,
        [
            [project_id, "alice", _minute(timedelta(minutes=2)), 3],
            [project_id, "bob", _minute(timedelta(minutes=2)), 0],
        ],
        column_names=["project_id", "user_id", "minute", "denials"],
    )

    assert _ranking(clickhouse, project_id, "1h", UsageMetric.DENIALS) == [("alice", 3)]


@pytest.mark.integration
def test_top_users_break_ties_by_user_id_and_honour_the_limit(
    clickhouse, project_id: str
) -> None:
    rows = [
        [project_id, user, _minute(timedelta(minutes=2)), 7, 0, 0]
        for user in ("zoe", "adam", "mia")
    ]
    clickhouse.insert(USAGE_MINUTE_USER_TABLE, rows, column_names=_USER_COLUMNS)

    tied = _ranking(clickhouse, project_id, "1h", UsageMetric.INVOCATIONS)
    first = _ranking(clickhouse, project_id, "1h", UsageMetric.INVOCATIONS, limit=1)

    assert tied == [("adam", 7), ("mia", 7), ("zoe", 7)]
    assert first == [("adam", 7)]
