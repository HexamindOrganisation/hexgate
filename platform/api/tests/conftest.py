"""Skip @pytest.mark.integration tests unless run with `pytest -m integration`."""
from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("-m") == "integration":
        return
    skip_integration = pytest.mark.skip(
        reason="opt-in: run with `pytest -m integration` (requires running ClickHouse)"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
