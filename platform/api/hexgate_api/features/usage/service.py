"""Read path for agent usage windows: one ``usage_minute`` scan, memoized per worker."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from types import MappingProxyType
from typing import Final

from clickhouse_connect.driver.client import Client

from hexgate_api.core.clickhouse import verify_written_columns
from hexgate_api.features.usage.paths import UsageMetric, UsageWindowSpec

USAGE_MINUTE_TABLE: Final = "usage_minute"
# Code-owned SQL per metric. No request string ever reaches the SQL text.
_METRIC_SQL: Final[Mapping[UsageMetric, str]] = MappingProxyType(
    {
        UsageMetric.INVOCATIONS: "invocations",
        UsageMetric.TOOL_CALLS: "tool_calls",
        UsageMetric.DENIALS: "denials",
        UsageMetric.LLM_CALLS: "llm_calls",
        UsageMetric.INPUT_TOKENS: "input_tokens",
        UsageMetric.OUTPUT_TOKENS: "output_tokens",
        UsageMetric.TOTAL_TOKENS: "input_tokens + output_tokens",
    }
)
_READ_COLUMNS: Final = [
    "project_id",
    "agent_name",
    "minute",
    "invocations",
    "tool_calls",
    "denials",
    "llm_calls",
    "input_tokens",
    "output_tokens",
]
# The SDK gives up after its 2 s refresh_timeout; don't run longer than that.
QUERY_SETTINGS: Final = {"max_execution_time": 2}
USAGE_MEMO_TTL_SECONDS: Final = 1.0
USAGE_MEMO_MAX_ENTRIES: Final = 10_000

_SCAN_PARAM: Final = "scan"
_WINDOW_PARAM_PREFIX: Final = "w"
_VALUE_ALIAS_PREFIX: Final = "v"


@dataclass(frozen=True, slots=True)
class UsageReadout:
    as_of: datetime
    values: Mapping[UsageWindowSpec, int]


def build_usage_query(
    project_id: str, agent_name: str, specs: Sequence[UsageWindowSpec]
) -> tuple[str, dict[str, object]]:
    """One ``sumIf`` column per spec, one parameter per distinct window, and a scan
    bounded by the largest. A window starts at ``toStartOfMinute(as_of - w)``, so the
    leading partial minute counts: an over-count of under a minute."""
    window_params = {
        seconds: f"{_WINDOW_PARAM_PREFIX}{i}"
        for i, seconds in enumerate(dict.fromkeys(s.window_seconds for s in specs))
    }
    columns = ",\n".join(
        f"    sumIf({_METRIC_SQL[spec.metric]}, "
        f"{_window_start_filter(window_params[spec.window_seconds])}) "
        f"AS {_VALUE_ALIAS_PREFIX}{i}"
        for i, spec in enumerate(specs)
    )
    sql = (
        "WITH now('UTC') AS as_of\n"
        "SELECT\n"
        "    as_of,\n"
        f"{columns}\n"
        f"FROM {USAGE_MINUTE_TABLE}\n"
        "WHERE project_id = {project_id:String}\n"
        "  AND agent_name = {agent_name:String}\n"
        f"  AND {_window_start_filter(_SCAN_PARAM)}"
    )
    params: dict[str, object] = {
        "project_id": project_id,
        "agent_name": agent_name,
        **{name: seconds for seconds, name in window_params.items()},
        _SCAN_PARAM: max(window_params),
    }
    return sql, params


def _window_start_filter(param: str) -> str:
    return f"minute >= toStartOfMinute(as_of - toIntervalSecond({{{param}:UInt32}}))"


def read_usage(
    client: Client, project_id: str, agent_name: str, specs: Sequence[UsageWindowSpec]
) -> UsageReadout:
    """Synchronous: the route runs it off the event loop."""
    sql, params = build_usage_query(project_id, agent_name, specs)
    as_of, *values = client.query(
        sql, parameters=params, settings=QUERY_SETTINGS
    ).result_rows[0]
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    return UsageReadout(
        as_of=as_of,
        values={spec: int(value) for spec, value in zip(specs, values, strict=True)},
    )


UsageMemoKey = tuple[str, str, frozenset[UsageWindowSpec]]


class UsageMemo:
    """Per-worker memo of usage reads; concurrent misses on one key share one query.

    Event-loop only: no lock, because every access happens on the loop thread."""

    def __init__(
        self, ttl_seconds: float, max_entries: int, clock: Callable[[], float]
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[
            UsageMemoKey, tuple[float, asyncio.Future[UsageReadout]]
        ] = {}

    async def get_or_load(
        self, key: UsageMemoKey, load: Callable[[], Awaitable[UsageReadout]]
    ) -> UsageReadout:
        entry = self._entries.get(key)
        if entry is not None and self._clock() < entry[0]:
            task = entry[1]
        else:
            task = self._start(key, load)
        # Shielded: a caller that disconnects mustn't cancel the shared read.
        return await asyncio.shield(task)

    def _start(
        self, key: UsageMemoKey, load: Callable[[], Awaitable[UsageReadout]]
    ) -> asyncio.Future[UsageReadout]:
        self._make_room()
        task = asyncio.ensure_future(load())
        self._entries[key] = (self._clock() + self._ttl, task)
        task.add_done_callback(lambda done: self._forget_if_failed(key, done))
        return task

    def _forget_if_failed(
        self, key: UsageMemoKey, task: asyncio.Future[UsageReadout]
    ) -> None:
        if not task.cancelled() and task.exception() is None:
            return
        entry = self._entries.get(key)
        if entry is not None and entry[1] is task:
            del self._entries[key]

    def _make_room(self) -> None:
        if len(self._entries) < self._max_entries:
            return
        now = self._clock()
        for key in [k for k, (expiry, _) in self._entries.items() if expiry <= now]:
            del self._entries[key]
        if len(self._entries) >= self._max_entries:
            del self._entries[next(iter(self._entries))]


@lru_cache
def get_usage_memo() -> UsageMemo:
    return UsageMemo(USAGE_MEMO_TTL_SECONDS, USAGE_MEMO_MAX_ENTRIES, time.monotonic)


def verify_schema(client: Client) -> None:
    """Raise SchemaOutOfDate if usage_minute (migration 0005) is missing a read column."""
    verify_written_columns(client, ((USAGE_MINUTE_TABLE, _READ_COLUMNS),))
