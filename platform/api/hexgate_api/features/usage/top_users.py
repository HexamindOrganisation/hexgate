"""Dashboard ranking of a project's end users by one usage metric, across every agent."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from clickhouse_connect.driver.client import Client

from hexgate_api.features.usage.paths import MAX_WINDOW_SECONDS, UsageMetric
from hexgate_api.features.usage.service import (
    METRIC_SQL,
    USER_ROLLUP,
    window_start_filter,
)

TopUsersWindow = Literal["1h", "24h", "7d", "30d"]
# Quota windows, all within the rollup's TTL (paths.MAX_WINDOW_SECONDS).
TOP_USERS_WINDOW_SECONDS: Final[Mapping[str, int]] = MappingProxyType(
    {"1h": 3_600, "24h": 86_400, "7d": 7 * 86_400, "30d": MAX_WINDOW_SECONDS}
)
TOP_USERS_DEFAULT_LIMIT: Final = 10
TOP_USERS_MAX_LIMIT: Final = 50
# A 30-day GROUP BY over a B2C project is the heaviest read on this table, and
# SDK user reads share the server with a 2 s budget: bound it.
TOP_USERS_READ_TIMEOUT_SECONDS: Final = 10
_QUERY_SETTINGS: Final = {"max_execution_time": TOP_USERS_READ_TIMEOUT_SECONDS}
_WINDOW_PARAM: Final = "window"


@dataclass(frozen=True, slots=True)
class TopUser:
    user_id: str
    value: int


def build_top_users_query(
    project_id: str, window_seconds: int, metric: UsageMetric, limit: int
) -> tuple[str, dict[str, object]]:
    """Users with a non-zero value only. Windows start as in build_usage_query."""
    sql = (
        "WITH now('UTC') AS as_of\n"
        "SELECT\n"
        f"    {USER_ROLLUP.key_column},\n"
        f"    sum({METRIC_SQL[metric]}) AS value\n"
        f"FROM {USER_ROLLUP.table}\n"
        "WHERE project_id = {project_id:String}\n"
        f"  AND {window_start_filter(_WINDOW_PARAM)}\n"
        f"GROUP BY {USER_ROLLUP.key_column}\n"
        "HAVING value > 0\n"
        f"ORDER BY value DESC, {USER_ROLLUP.key_column} ASC\n"
        "LIMIT {limit:UInt32}"
    )
    params: dict[str, object] = {
        "project_id": project_id,
        _WINDOW_PARAM: window_seconds,
        "limit": limit,
    }
    return sql, params


def read_top_users(
    client: Client,
    project_id: str,
    window_seconds: int,
    metric: UsageMetric,
    limit: int,
) -> list[TopUser]:
    """Synchronous: the route runs it off the event loop."""
    sql, params = build_top_users_query(project_id, window_seconds, metric, limit)
    rows = client.query(sql, parameters=params, settings=_QUERY_SETTINGS).result_rows
    return [TopUser(user_id=user_id, value=int(value)) for user_id, value in rows]
