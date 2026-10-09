"""Tests for the platform term of ``agent_usage.*`` — ``hexgate.security.usage_source``.

A fake fetcher, a fake clock and fake executors, so nothing sleeps or races. The
gates pinned here fail silently in production: G3 (fail open by default), G4 / F5
(a path missing from the snapshot never denies the rest) and G7 (the ingest margin).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from hexgate.cloud.client import HexgateError
from hexgate.runtime.agent_usage import UsageLedger, UsageMetric, new_usage_ledger
from hexgate.security import usage_source
from hexgate.security.usage_source import (
    DEFAULT_MAX_STALENESS_SECONDS,
    DEFAULT_MIN_REFRESH_SECONDS,
    DEFAULT_SYNC_AFTER_SECONDS,
    USAGE_INGEST_MARGIN_SECONDS,
    OnUnavailable,
    PlatformUsageSource,
    UsageContentError,
    UsageRefreshSettings,
    UsageState,
    get_usage_source,
    local_reading,
    resolve_usage_source,
    usage_values_from_payload,
)
from hexgate.tracing import _senders

_START = 1_000.0
_MIN_REFRESH = 5.0
_SYNC_AFTER = 60.0
_MAX_STALENESS = 300.0
_BETWEEN_TIERS = 10.0
_AGENT = "billing"
_HOUR = "invocations_1h"
_FIVE_MIN = "tool_calls_5m"
_ONE_PATH = frozenset({_HOUR})
_TWO_PATHS = frozenset({_HOUR, _FIVE_MIN})
_PLATFORM_VALUES = {_HOUR: 7, _FIVE_MIN: 3}
_SETTINGS = UsageRefreshSettings(_MIN_REFRESH, _SYNC_AFTER, _MAX_STALENESS)
_ENV_VARS = (
    "HEXGATE_API_KEY",
    "HEXGATE_API_URL",
    "HEXGATE_LOCAL_POLICY",
    "HEXGATE_USAGE_REFRESH_SECONDS",
    "HEXGATE_USAGE_SYNC_AFTER_SECONDS",
    "HEXGATE_USAGE_MAX_STALENESS_SECONDS",
    _senders._LOCAL_MODE_ENV,
)


@pytest.fixture(autouse=True)
def _isolate_usage_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    usage_source._usage_sources.clear()
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield
    usage_source._usage_sources.clear()


class _FakeClock:
    def __init__(self, now: float = _START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FakeFetcher:
    """Answers with ``values`` (requested or not), or raises ``error``. Records the
    calling thread, and can advance the clock to model a slow fetch."""

    def __init__(self, clock: _FakeClock, values: Mapping[str, int]) -> None:
        self.clock = clock
        self.values = dict(values)
        self.error: Exception | None = None
        self.body: Any = None
        self.advance = 0.0
        self.calls: list[tuple[str, list[str], str]] = []

    def get_agent_usage(self, name: str, paths: Sequence[str]) -> Mapping[str, Any]:
        self.calls.append((name, list(paths), threading.current_thread().name))
        self.clock.now += self.advance
        if self.error is not None:
            raise self.error
        if self.body is not None:
            return self.body
        return {"as_of": "2026-10-09T00:00:00Z", "values": dict(self.values)}


class _InlineExecutor(Executor):
    """Runs each job on submit, on the caller's thread."""

    def submit(  # type: ignore[override]
        self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Future[Any]:
        future: Future[Any] = Future()
        future.set_result(fn(*args, **kwargs))
        return future


class _HeldExecutor(Executor):
    """Holds each job until :meth:`run_all`, so a test sees what was scheduled."""

    def __init__(self) -> None:
        self.jobs: list[tuple[Future[Any], Callable[[], Any]]] = []

    def submit(  # type: ignore[override]
        self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Future[Any]:
        future: Future[Any] = Future()
        self.jobs.append((future, lambda: fn(*args, **kwargs)))
        return future

    def run_all(self) -> None:
        jobs, self.jobs = self.jobs, []
        for future, job in jobs:
            future.set_result(job())


class _ClosedExecutor(Executor):
    def submit(  # type: ignore[override]
        self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Future[Any]:
        raise RuntimeError("cannot schedule new futures after interpreter shutdown")


@dataclass
class _Rig:
    clock: _FakeClock
    fetcher: _FakeFetcher
    source: PlatformUsageSource
    ledger: UsageLedger


def _rig(
    executor: Executor | None = None,
    values: Mapping[str, int] = _PLATFORM_VALUES,
) -> _Rig:
    clock = _FakeClock()
    fetcher = _FakeFetcher(clock, values)
    executor = executor if executor is not None else _InlineExecutor()
    source = PlatformUsageSource(fetcher, executor, _SETTINGS, clock)
    return _Rig(clock, fetcher, source, new_usage_ledger(clock=clock))


def _seeded(
    executor: Executor | None = None,
    paths: frozenset[str] = _ONE_PATH,
    values: Mapping[str, int] = _PLATFORM_VALUES,
) -> _Rig:
    """A rig whose snapshot was fetched at ``_START`` for ``paths``."""
    rig = _rig(executor, values)
    rig.source.refresh(_AGENT, paths)
    assert len(rig.fetcher.calls) == 1
    return rig


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


def test_a_valid_body_yields_the_requested_values() -> None:
    payload = {"as_of": "x", "values": {_HOUR: 4, "denials_1h": 9}}

    assert usage_values_from_payload(payload, _TWO_PATHS) == {_HOUR: 4}


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"as_of": "x"},
        {"values": [1]},
        {"values": {_HOUR: -1}},
        {"values": {_HOUR: True}},
        {"values": {_HOUR: 1.5}},
    ],
    ids=["not-a-dict", "no-values", "values-not-a-dict", "negative", "bool", "float"],
)
def test_a_malformed_body_is_rejected_whole(payload: Any) -> None:
    with pytest.raises(UsageContentError):
        usage_values_from_payload(payload, _ONE_PATH)


def test_a_bad_value_on_an_unrequested_key_is_ignored() -> None:
    payload = {"values": {_HOUR: 2, "denials_1h": -1}}

    assert usage_values_from_payload(payload, _ONE_PATH) == {_HOUR: 2}


# ---------------------------------------------------------------------------
# Refresh tiers
# ---------------------------------------------------------------------------


def test_a_cold_agent_fetches_synchronously_on_the_callers_thread() -> None:
    executor = _HeldExecutor()
    rig = _rig(executor)

    rig.source.refresh(_AGENT, _TWO_PATHS)

    assert rig.fetcher.calls == [
        (_AGENT, sorted(_TWO_PATHS), threading.current_thread().name)
    ]
    assert executor.jobs == []


def test_a_young_snapshot_is_used_as_is() -> None:
    rig = _seeded()
    rig.clock.now += _MIN_REFRESH - 1

    rig.source.refresh(_AGENT, _ONE_PATH)

    assert len(rig.fetcher.calls) == 1


def test_a_snapshot_between_tiers_refreshes_in_the_background() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)
    rig.clock.now += _BETWEEN_TIERS

    rig.source.refresh(_AGENT, _ONE_PATH)

    assert len(rig.fetcher.calls) == 1
    assert len(executor.jobs) == 1
    executor.run_all()
    assert len(rig.fetcher.calls) == 2


def test_a_snapshot_past_sync_after_fetches_synchronously() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)
    rig.clock.now += _SYNC_AFTER

    rig.source.refresh(_AGENT, _ONE_PATH)

    assert len(rig.fetcher.calls) == 2
    assert executor.jobs == []


def test_refresh_with_no_paths_never_fetches() -> None:
    rig = _rig()

    rig.source.refresh(_AGENT, frozenset())

    assert rig.fetcher.calls == []


def test_a_new_path_refreshes_in_the_background_without_blocking() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)

    rig.source.refresh(_AGENT, _TWO_PATHS)

    assert len(rig.fetcher.calls) == 1
    executor.run_all()
    assert rig.fetcher.calls[-1][1] == sorted(_TWO_PATHS)


def test_background_refreshes_are_single_flight() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)
    rig.clock.now += _BETWEEN_TIERS

    rig.source.refresh(_AGENT, _ONE_PATH)
    rig.source.refresh(_AGENT, _ONE_PATH)
    rig.source.read(_AGENT, _TWO_PATHS, rig.ledger)

    assert len(executor.jobs) == 1


def test_a_finished_background_refresh_frees_the_slot() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)
    rig.clock.now += _BETWEEN_TIERS
    rig.source.refresh(_AGENT, _ONE_PATH)
    executor.run_all()

    rig.clock.now += _BETWEEN_TIERS
    rig.source.refresh(_AGENT, _ONE_PATH)

    assert len(executor.jobs) == 1


def test_a_background_fetch_asks_for_the_paths_wanted_when_it_runs() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)
    rig.clock.now += _BETWEEN_TIERS
    rig.source.refresh(_AGENT, _ONE_PATH)

    rig.source.refresh(_AGENT, _TWO_PATHS)
    executor.run_all()

    assert rig.fetcher.calls[-1][1] == sorted(_TWO_PATHS)


def test_a_closed_executor_never_reaches_the_reader() -> None:
    rig = _seeded(_ClosedExecutor())
    rig.clock.now += _BETWEEN_TIERS

    rig.source.refresh(_AGENT, _ONE_PATH)
    reading = rig.source.read(_AGENT, _TWO_PATHS, rig.ledger)

    assert reading.state is UsageState.PARTIAL


# ---------------------------------------------------------------------------
# Throttle and fail-soft
# ---------------------------------------------------------------------------


def test_a_failed_fetch_is_not_retried_within_min_refresh() -> None:
    rig = _rig()
    rig.fetcher.error = HexgateError("platform down", status=503)
    rig.source.refresh(_AGENT, _ONE_PATH)

    rig.clock.now += _MIN_REFRESH - 1
    rig.source.refresh(_AGENT, _ONE_PATH)
    rig.source.read(_AGENT, _ONE_PATH, rig.ledger)
    assert len(rig.fetcher.calls) == 1

    rig.clock.now += 1
    rig.source.refresh(_AGENT, _ONE_PATH)
    assert len(rig.fetcher.calls) == 2


def test_a_new_path_set_bypasses_the_throttle() -> None:
    rig = _rig()
    rig.fetcher.error = HexgateError("platform down", status=503)
    rig.source.refresh(_AGENT, _ONE_PATH)

    rig.source.refresh(_AGENT, _TWO_PATHS)

    assert len(rig.fetcher.calls) == 2


def test_a_failed_fetch_keeps_last_good_and_reads_stale() -> None:
    rig = _seeded()
    rig.fetcher.error = HexgateError("platform down", status=503)
    rig.clock.now += _SYNC_AFTER

    rig.source.refresh(_AGENT, _ONE_PATH)
    reading = rig.source.read(_AGENT, _ONE_PATH, rig.ledger)

    assert len(rig.fetcher.calls) == 2
    assert reading == usage_source.UsageReading({_HOUR: 7}, UsageState.STALE)


def test_a_content_error_keeps_last_good_and_logs_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _seeded()
    rig.fetcher.body = {"values": [1]}
    rig.clock.now += _SYNC_AFTER

    with caplog.at_level(logging.WARNING, logger=usage_source.logger.name):
        rig.source.refresh(_AGENT, _ONE_PATH)

    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    assert rig.source.read(_AGENT, _ONE_PATH, rig.ledger).namespace == {_HOUR: 7}


def test_an_unregistered_agent_is_reported_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _rig()
    rig.fetcher.error = HexgateError("not found", status=404)

    with caplog.at_level(logging.WARNING, logger=usage_source.logger.name):
        rig.source.refresh(_AGENT, _ONE_PATH)
        rig.clock.now += _MIN_REFRESH
        rig.source.refresh(_AGENT, _ONE_PATH)

    assert len(rig.fetcher.calls) == 2
    assert len(caplog.records) == 1
    assert "not registered" in caplog.records[0].getMessage()


def test_any_fetch_error_is_swallowed() -> None:
    rig = _rig()
    rig.fetcher.error = OSError("boom")

    rig.source.refresh(_AGENT, _ONE_PATH)

    assert rig.source.read(_AGENT, _ONE_PATH, rig.ledger).state is (
        UsageState.UNAVAILABLE
    )


def test_the_snapshot_is_stamped_when_the_fetch_starts() -> None:
    rig = _rig()
    rig.fetcher.advance = 1.5

    rig.source.refresh(_AGENT, _ONE_PATH)

    assert rig.source._snapshots[_AGENT].fetched_at == _START


# ---------------------------------------------------------------------------
# read: states, fail mode, and the gates
# ---------------------------------------------------------------------------


def test_a_fresh_snapshot_adds_the_local_term() -> None:
    rig = _seeded()
    rig.clock.now += 1
    rig.ledger.record({UsageMetric.INVOCATIONS: 2})

    reading = rig.source.read(_AGENT, _ONE_PATH, rig.ledger)

    assert reading == usage_source.UsageReading({_HOUR: 9}, UsageState.FRESH)


def test_an_event_inside_the_ingest_margin_is_counted_once_more() -> None:
    """G7: recorded just before the fetch and not yet in the platform's value."""
    rig = _rig()
    rig.clock.now -= USAGE_INGEST_MARGIN_SECONDS / 3
    rig.ledger.record({UsageMetric.INVOCATIONS: 1})
    rig.clock.now = _START
    rig.source.refresh(_AGENT, _ONE_PATH)

    reading = rig.source.read(_AGENT, _ONE_PATH, rig.ledger)

    assert reading.namespace == {_HOUR: _PLATFORM_VALUES[_HOUR] + 1}


@pytest.mark.parametrize(
    "seed",
    [False, True],
    ids=["no-snapshot", "older-than-max-staleness"],
)
def test_an_unavailable_snapshot_fails_open_by_default(seed: bool) -> None:
    """G3: ledger-only values, never an absent namespace."""
    rig = _seeded(_HeldExecutor()) if seed else _rig(_HeldExecutor())
    rig.clock.now += _MAX_STALENESS
    rig.ledger.record({UsageMetric.INVOCATIONS: 2})

    reading = rig.source.read(_AGENT, _ONE_PATH, rig.ledger)

    assert reading == usage_source.UsageReading({_HOUR: 2}, UsageState.UNAVAILABLE)


def test_an_unavailable_snapshot_under_deny_reads_nothing() -> None:
    rig = _rig(_HeldExecutor())

    reading = rig.source.read(
        _AGENT, _ONE_PATH, rig.ledger, on_unavailable=OnUnavailable.DENY
    )

    assert reading == usage_source.UsageReading(None, UsageState.UNAVAILABLE)


def test_a_path_missing_from_the_snapshot_reads_ledger_only() -> None:
    """G4 / F5: a policy adding a path must not deny every run until a refresh."""
    executor = _HeldExecutor()
    rig = _seeded(executor)
    rig.ledger.record({UsageMetric.TOOL_CALLS: 1})

    reading = rig.source.read(_AGENT, _TWO_PATHS, rig.ledger)

    assert reading == usage_source.UsageReading(
        {_HOUR: 7, _FIVE_MIN: 1}, UsageState.PARTIAL
    )
    executor.run_all()
    assert rig.fetcher.calls[-1][1] == sorted(_TWO_PATHS)


def test_a_path_missing_from_the_snapshot_under_deny_fails_only_itself() -> None:
    rig = _seeded(_HeldExecutor())

    reading = rig.source.read(
        _AGENT, _TWO_PATHS, rig.ledger, on_unavailable=OnUnavailable.DENY
    )

    assert reading == usage_source.UsageReading({_HOUR: 7}, UsageState.PARTIAL)


def test_a_path_the_platform_left_out_reads_partial() -> None:
    rig = _seeded(_HeldExecutor(), paths=_TWO_PATHS, values={_HOUR: 7})

    reading = rig.source.read(_AGENT, _TWO_PATHS, rig.ledger)

    assert reading.state is UsageState.PARTIAL
    assert reading.namespace == {_HOUR: 7, _FIVE_MIN: 0}


def test_the_current_run_is_left_out_when_it_is_in_the_local_term() -> None:
    rig = _seeded()
    rig.clock.now += 1
    rig.ledger.record({UsageMetric.INVOCATIONS: 1})

    reading = rig.source.read(_AGENT, _ONE_PATH, rig.ledger, current_run_age=0.0)

    assert reading.namespace == {_HOUR: 7}


def test_read_never_fetches_on_the_callers_thread() -> None:
    executor = _HeldExecutor()
    rig = _rig(executor)

    rig.source.read(_AGENT, _ONE_PATH, rig.ledger)

    assert rig.fetcher.calls == []
    assert len(executor.jobs) == 1


def test_a_fresh_read_schedules_nothing() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)

    rig.source.read(_AGENT, _ONE_PATH, rig.ledger)

    assert executor.jobs == []


def test_local_reading_is_the_ledger_alone() -> None:
    ledger = new_usage_ledger(clock=_FakeClock())
    ledger.record({UsageMetric.INVOCATIONS: 3})

    assert local_reading(ledger, _ONE_PATH) == usage_source.UsageReading(
        {_HOUR: 3}, UsageState.LOCAL
    )


# ---------------------------------------------------------------------------
# refresh_async
# ---------------------------------------------------------------------------


class _ToThreadSpy:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, fn: Callable[..., Any], /, *args: Any) -> Any:
        self.calls += 1
        return fn(*args)


async def test_refresh_async_fetches_off_the_loop_only_when_synchronous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _ToThreadSpy()
    monkeypatch.setattr(asyncio, "to_thread", spy)
    executor = _HeldExecutor()
    rig = _rig(executor)

    await rig.source.refresh_async(_AGENT, _ONE_PATH)
    assert (spy.calls, len(rig.fetcher.calls)) == (1, 1)

    rig.clock.now += _BETWEEN_TIERS
    await rig.source.refresh_async(_AGENT, _ONE_PATH)
    assert (spy.calls, len(executor.jobs)) == (1, 1)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "thresholds",
    [(0.0, 60.0, 300.0), (61.0, 60.0, 300.0), (5.0, 301.0, 300.0)],
    ids=["zero-min-refresh", "min-after-sync", "sync-after-max"],
)
def test_out_of_order_thresholds_are_refused(
    thresholds: tuple[float, float, float],
) -> None:
    with pytest.raises(ValueError, match="thresholds"):
        UsageRefreshSettings(*thresholds)


def test_settings_are_read_from_the_environment() -> None:
    environ = {
        "HEXGATE_USAGE_REFRESH_SECONDS": "2",
        "HEXGATE_USAGE_SYNC_AFTER_SECONDS": "30",
        "HEXGATE_USAGE_MAX_STALENESS_SECONDS": "120",
    }

    assert UsageRefreshSettings.from_env(environ) == UsageRefreshSettings(
        2.0, 30.0, 120.0
    )


@pytest.mark.parametrize(
    "environ",
    [
        {"HEXGATE_USAGE_REFRESH_SECONDS": "soon"},
        {"HEXGATE_USAGE_SYNC_AFTER_SECONDS": "900"},
    ],
    ids=["unparsable", "out-of-order"],
)
def test_bad_settings_fall_back_to_the_defaults(
    environ: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=usage_source.logger.name):
        settings = UsageRefreshSettings.from_env(environ)

    assert settings == UsageRefreshSettings(
        DEFAULT_MIN_REFRESH_SECONDS,
        DEFAULT_SYNC_AFTER_SECONDS,
        DEFAULT_MAX_STALENESS_SECONDS,
    )
    assert len(caplog.records) == 1


# ---------------------------------------------------------------------------
# Registry and resolver
# ---------------------------------------------------------------------------


def _client(base_url: str = "http://platform") -> Any:
    return SimpleNamespace(config=SimpleNamespace(base_url=base_url))


def test_one_source_per_key_and_platform() -> None:
    client = _client()

    assert get_usage_source("k", client) is get_usage_source("k", client)
    assert get_usage_source("k", client) is not get_usage_source("other", client)
    assert get_usage_source("k", client) is not get_usage_source(
        "k", _client("http://staging")
    )


def test_resolve_returns_the_shared_source_when_a_key_is_set() -> None:
    client = _client()

    assert resolve_usage_source(api_key="k", client=client) is get_usage_source(
        "k", client
    )


@pytest.mark.parametrize(
    ("env", "api_key"),
    [
        ({_senders._LOCAL_MODE_ENV: "1"}, "k"),
        ({"HEXGATE_LOCAL_POLICY": "policy.yaml"}, "k"),
        ({}, None),
    ],
    ids=["local-mode", "local-policy", "no-key"],
)
def test_resolve_returns_none_without_a_platform(
    env: dict[str, str], api_key: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert resolve_usage_source(api_key=api_key, client=_client()) is None
