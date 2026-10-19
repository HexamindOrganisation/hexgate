"""The top-users query builder and row mapping, with no database."""

from __future__ import annotations

from typing import get_args
from unittest.mock import MagicMock

import pytest

from hexgate_api.features.usage.paths import MAX_WINDOW_SECONDS, UsageMetric
from hexgate_api.features.usage.service import METRIC_SQL, USER_ROLLUP
from hexgate_api.features.usage.top_users import (
    TOP_USERS_READ_TIMEOUT_SECONDS,
    TOP_USERS_WINDOW_SECONDS,
    TopUser,
    TopUsersWindow,
    build_top_users_query,
    read_top_users,
)

_PROJECT = "proj_top"
_DAY = 86_400
_LIMIT = 10


@pytest.mark.parametrize("metric", list(UsageMetric))
def test_each_metric_sums_its_code_owned_expression(metric: UsageMetric) -> None:
    sql, _ = build_top_users_query(_PROJECT, _DAY, metric, _LIMIT)

    assert f"sum({METRIC_SQL[metric]}) AS value" in sql
    assert _PROJECT not in sql


def test_request_values_are_bound_and_names_come_from_the_rollup() -> None:
    sql, params = build_top_users_query(_PROJECT, _DAY, UsageMetric.DENIALS, _LIMIT)

    assert params == {"project_id": _PROJECT, "window": _DAY, "limit": _LIMIT}
    assert f"FROM {USER_ROLLUP.table}\n" in sql
    assert f"GROUP BY {USER_ROLLUP.key_column}\n" in sql


def test_the_ranking_is_deterministic_and_skips_zeros() -> None:
    sql, _ = build_top_users_query(_PROJECT, _DAY, UsageMetric.DENIALS, _LIMIT)

    assert f"ORDER BY value DESC, {USER_ROLLUP.key_column} ASC" in sql
    assert "HAVING value > 0" in sql


def test_every_window_is_within_the_rollup_ttl() -> None:
    assert set(TOP_USERS_WINDOW_SECONDS) == set(get_args(TopUsersWindow))
    assert all(s <= MAX_WINDOW_SECONDS for s in TOP_USERS_WINDOW_SECONDS.values())


def test_read_maps_rows_and_bounds_the_query() -> None:
    client = MagicMock()
    client.query.return_value.result_rows = [("a", 5), ("b", 3.0)]

    users = read_top_users(client, _PROJECT, _DAY, UsageMetric.INVOCATIONS, _LIMIT)

    assert users == [TopUser("a", 5), TopUser("b", 3)]
    assert all(type(u.value) is int for u in users)
    settings = client.query.call_args.kwargs["settings"]
    assert settings == {"max_execution_time": TOP_USERS_READ_TIMEOUT_SECONDS}
