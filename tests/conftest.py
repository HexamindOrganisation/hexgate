"""Shared pytest fixtures for the fortify test suite."""

from __future__ import annotations

import pytest

from fortify.runtime.srt import SrtUnavailableError, ensure_srt_available


@pytest.fixture
def srt_required() -> None:
    """Skip the test when the `srt` binary is not installed.

    Use for true integration tests that actually spawn `srt`. Pure unit
    tests should mock `ensure_srt_available` and `asyncio.create_subprocess_exec`
    instead so they run anywhere.
    """
    try:
        ensure_srt_available()
    except SrtUnavailableError as error:
        pytest.skip(f"srt not installed; {error}")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip @pytest.mark.integration tests unless run with `pytest -m integration`."""
    if config.getoption("-m") == "integration":
        return
    skip_integration = pytest.mark.skip(
        reason="opt-in: run with `pytest -m integration`"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
