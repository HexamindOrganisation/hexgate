"""Shared pytest fixtures for the hexgate test suite."""

from __future__ import annotations

import os
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from hexgate.cloud.client import HexgateClient
from hexgate.runtime.srt import SrtUnavailableError, ensure_srt_available
from hexgate.security.enforcer import PolicyEnforcer
from hexgate.tracing import _senders


@pytest.fixture(autouse=True)
def _no_network_span_export(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep every ``AuditSender`` a unit test builds off the network.

    ``AuditSender()`` otherwise wires a real OTLP/HTTP exporter, and any span
    a test emits through a registry sender is POSTed to the default endpoint
    (the production Collector) when the sender is flushed or shut down.
    Swapping the exporter factory for an in-memory one keeps the sender's
    full pipeline (provider → batch processor → exporter) intact while
    making the export a no-op. Integration tests opt out (they exist to hit
    the live pipeline), as does anything marked ``real_span_exporter``."""
    if "integration" in request.keywords or "real_span_exporter" in request.keywords:
        return
    monkeypatch.setattr(
        _senders.AuditSender, "_new_exporter", lambda self: InMemorySpanExporter()
    )


@pytest.fixture
def enforcer_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[ModuleType], list[HexgateClient | None]]:
    """Spy on ``build_enforcer`` as ``module`` imported it: returns the list each
    call's ``client=`` is appended to, while the real enforcer is still built."""

    def spy_on(module: ModuleType) -> list[HexgateClient | None]:
        clients: list[HexgateClient | None] = []
        real_build_enforcer = module.build_enforcer

        def spy_build_enforcer(*args: Any, **kwargs: Any) -> PolicyEnforcer:
            clients.append(kwargs.get("client"))
            return real_build_enforcer(*args, **kwargs)

        monkeypatch.setattr(module, "build_enforcer", spy_build_enforcer)
        return clients

    return spy_on


def pytest_configure(config: pytest.Config) -> None:
    """Silence Langfuse's background span export when no real project
    credentials are configured.

    ``Langfuse()`` defaults ``tracing_enabled=True`` even with no/empty
    public+secret key, so any ``@observe``-wrapped ``@agent_tool`` call in a
    unit test still queues spans and tries to flush them against the real
    Langfuse host — failing with a 401 that has nothing to do with the test
    itself. Must run in ``pytest_configure`` (before collection), not a
    per-test fixture: whatever first constructs the client during this suite
    does so early enough that a function-scoped ``monkeypatch.setenv`` in an
    autouse fixture is already too late to gate it. Leaves tracing untouched
    when a real key is already in the environment (e.g. `pytest -m
    integration` with `.env` sourced), so that path still exercises the real
    client.
    """
    _ = config
    if not (
        os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")
    ):
        os.environ["LANGFUSE_TRACING_ENABLED"] = "false"


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


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip @pytest.mark.integration tests unless run with `pytest -m integration`."""
    if config.getoption("-m") == "integration":
        return
    skip_integration = pytest.mark.skip(
        reason="opt-in: run with `pytest -m integration`"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
