"""Test-suite-wide pytest hooks.

The audit suite has integration tests that need a running ClickHouse
(via `make clickhouse-up`). Treat the `integration` marker as opt-in so
that the default `make platform-api-test` stays fast and offline-friendly.

Run integration tests with `pytest -m integration`. Any other `-m` value
or no `-m` at all leaves them skipped.
"""
from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("-m") == "integration":
        return  # explicit opt-in — let pytest's own marker filter run them
    skip_integration = pytest.mark.skip(
        reason="opt-in: run with `pytest -m integration` (requires running ClickHouse)"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
