"""Skip @pytest.mark.integration tests unless run with `pytest -m integration`."""
from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Substring, not equality: the user may select integration tests via a
    # compound expression like `-m "integration and not slow"`. If integration
    # is named at all, let pytest's own marker filtering decide what runs.
    if "integration" in config.getoption("-m"):
        return
    skip_integration = pytest.mark.skip(
        reason="opt-in: run with `pytest -m integration` (requires running ClickHouse)"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
