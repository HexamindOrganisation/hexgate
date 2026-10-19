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
from hexgate.runtime.agent_usage import (
    USER_CACHE_ENV,
    UsageLedger,
    UsageMetric,
    UsageScope,
    UsageScopeKind,
    new_usage_ledger,
)
from hexgate.security import usage_source
from hexgate.security.usage_source import (
    DEFAULT_MAX_STALENESS_SECONDS,
    DEFAULT_MIN_REFRESH_SECONDS,
    DEFAULT_SYNC_AFTER_SECONDS,
    DEFAULT_USER_MIN_REFRESH_SECONDS,
    REJECTED_RETRY_SECONDS,
    USAGE_INGEST_MARGIN_SECONDS,
    USER_ID_MAX_LENGTH,
    USER_REFRESH_DEFAULTS,
    USER_REFRESH_ENV,
    OnUnavailable,
    PlatformUsageSource,
    ScopeFetcher,
    ScopeKindSpec,
    UsageContentError,
    UsageRefreshSettings,
    UsageState,
    get_usage_source,
    local_reading,
    resolve_usage_source,
    usage_values_from_payload,
    user_id_is_readable,
)
from hexgate.tracing import _senders

_START = 1_000.0
_MIN_REFRESH = 5.0
_SYNC_AFTER = 60.0
_MAX_STALENESS = 300.0
_BETWEEN_TIERS = 10.0
_AGENT = "billing"
_SCOPE = UsageScope.agent(_AGENT)
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
    USER_REFRESH_ENV.min_refresh,
    USER_REFRESH_ENV.sync_after,
    USER_REFRESH_ENV.max_staleness,
    USER_CACHE_ENV,
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
        # Runs inside the fetch, to model another fetch overlapping this one.
        self.during: Callable[[str], None] | None = None
        self.calls: list[tuple[str, list[str], str]] = []

    def fetch(self, key: str, paths: Sequence[str]) -> dict[str, Any]:
        self.calls.append((key, list(paths), threading.current_thread().name))
        self.clock.now += self.advance
        if self.during is not None:
            during, self.during = self.during, None
            during(key)
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


_USER_VALUES = {_HOUR: 2, _FIVE_MIN: 1}


@dataclass
class _Rig:
    clock: _FakeClock
    fetcher: _FakeFetcher
    user_fetcher: _FakeFetcher  # registered only when the rig fetches users
    source: PlatformUsageSource
    ledger: UsageLedger


def _rig(
    executor: Executor | None = None,
    values: Mapping[str, int] = _PLATFORM_VALUES,
    *,
    fetch_users: bool = False,
    user_spec: Callable[[ScopeFetcher], ScopeKindSpec] = lambda fetch: ScopeKindSpec(
        fetch, _SETTINGS
    ),
) -> _Rig:
    clock = _FakeClock()
    fetcher = _FakeFetcher(clock, values)
    user_fetcher = _FakeFetcher(clock, _USER_VALUES)
    kinds = {UsageScopeKind.AGENT: ScopeKindSpec(fetcher.fetch, _SETTINGS)}
    if fetch_users:
        kinds[UsageScopeKind.USER] = user_spec(user_fetcher.fetch)
    executor = executor if executor is not None else _InlineExecutor()
    source = PlatformUsageSource(kinds, executor, clock)
    return _Rig(clock, fetcher, user_fetcher, source, new_usage_ledger(clock=clock))


def _user_rig(executor: Executor | None = None, *, capacity: int = 2) -> _Rig:
    """Users as the shared source keeps them: bounded, paused together, pre-checked,
    refreshed at 30 / 60 / 300."""
    return _rig(
        executor,
        fetch_users=True,
        user_spec=lambda fetch: ScopeKindSpec(
            fetch,
            USER_REFRESH_DEFAULTS,
            capacity=capacity,
            shared_backoff=True,
            accepts=user_id_is_readable,
        ),
    )


def _seeded(
    executor: Executor | None = None,
    paths: frozenset[str] = _ONE_PATH,
    values: Mapping[str, int] = _PLATFORM_VALUES,
) -> _Rig:
    """A rig whose snapshot was fetched at ``_START`` for ``paths``."""
    rig = _rig(executor, values)
    rig.source.refresh(_SCOPE, paths)
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

    rig.source.refresh(_SCOPE, _TWO_PATHS)

    assert rig.fetcher.calls == [
        (_AGENT, sorted(_TWO_PATHS), threading.current_thread().name)
    ]
    assert executor.jobs == []


def test_a_young_snapshot_is_used_as_is() -> None:
    rig = _seeded()
    rig.clock.now += _MIN_REFRESH - 1

    rig.source.refresh(_SCOPE, _ONE_PATH)

    assert len(rig.fetcher.calls) == 1


def test_a_snapshot_between_tiers_refreshes_in_the_background() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)
    rig.clock.now += _BETWEEN_TIERS

    rig.source.refresh(_SCOPE, _ONE_PATH)

    assert len(rig.fetcher.calls) == 1
    assert len(executor.jobs) == 1
    executor.run_all()
    assert len(rig.fetcher.calls) == 2


def test_a_snapshot_past_sync_after_fetches_synchronously() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)
    rig.clock.now += _SYNC_AFTER

    rig.source.refresh(_SCOPE, _ONE_PATH)

    assert len(rig.fetcher.calls) == 2
    assert executor.jobs == []


def test_refresh_with_no_paths_never_fetches() -> None:
    rig = _rig()

    rig.source.refresh(_SCOPE, frozenset())

    assert rig.fetcher.calls == []


def test_a_new_path_refreshes_in_the_background_without_blocking() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)

    rig.source.refresh(_SCOPE, _TWO_PATHS)

    assert len(rig.fetcher.calls) == 1
    executor.run_all()
    assert rig.fetcher.calls[-1][1] == sorted(_TWO_PATHS)


def test_background_refreshes_are_single_flight() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)
    rig.clock.now += _BETWEEN_TIERS

    rig.source.refresh(_SCOPE, _ONE_PATH)
    rig.source.refresh(_SCOPE, _ONE_PATH)
    rig.source.read(_SCOPE, _TWO_PATHS, rig.ledger)

    assert len(executor.jobs) == 1


def test_a_finished_background_refresh_frees_the_slot() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)
    rig.clock.now += _BETWEEN_TIERS
    rig.source.refresh(_SCOPE, _ONE_PATH)
    executor.run_all()

    rig.clock.now += _BETWEEN_TIERS
    rig.source.refresh(_SCOPE, _ONE_PATH)

    assert len(executor.jobs) == 1


def test_a_background_fetch_asks_for_the_paths_wanted_when_it_runs() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)
    rig.clock.now += _BETWEEN_TIERS
    rig.source.refresh(_SCOPE, _ONE_PATH)

    rig.source.refresh(_SCOPE, _TWO_PATHS)
    executor.run_all()

    assert rig.fetcher.calls[-1][1] == sorted(_TWO_PATHS)


def test_callers_wanting_different_paths_share_one_snapshot() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)
    other = frozenset({_FIVE_MIN})

    rig.source.read(_SCOPE, other, rig.ledger)
    executor.run_all()
    readings = [
        rig.source.read(_SCOPE, paths, rig.ledger) for paths in (_ONE_PATH, other)
    ]

    assert rig.fetcher.calls[-1][1] == sorted(_TWO_PATHS)
    assert [reading.state for reading in readings] == [UsageState.FRESH] * 2
    assert executor.jobs == []


def test_a_closed_executor_never_reaches_the_reader() -> None:
    rig = _seeded(_ClosedExecutor())
    rig.clock.now += _BETWEEN_TIERS

    rig.source.refresh(_SCOPE, _ONE_PATH)
    reading = rig.source.read(_SCOPE, _TWO_PATHS, rig.ledger)

    assert reading.state is UsageState.PARTIAL


# ---------------------------------------------------------------------------
# Throttle and fail-soft
# ---------------------------------------------------------------------------


def test_a_failed_fetch_is_not_retried_within_min_refresh() -> None:
    rig = _rig()
    rig.fetcher.error = HexgateError("platform down", status=503)
    rig.source.refresh(_SCOPE, _ONE_PATH)

    rig.clock.now += _MIN_REFRESH - 1
    rig.source.refresh(_SCOPE, _ONE_PATH)
    rig.source.read(_SCOPE, _ONE_PATH, rig.ledger)
    assert len(rig.fetcher.calls) == 1

    rig.clock.now += 1
    rig.source.refresh(_SCOPE, _ONE_PATH)
    assert len(rig.fetcher.calls) == 2


def test_a_new_path_set_bypasses_the_throttle() -> None:
    rig = _rig()
    rig.fetcher.error = HexgateError("platform down", status=503)
    rig.source.refresh(_SCOPE, _ONE_PATH)

    rig.source.refresh(_SCOPE, _TWO_PATHS)

    assert len(rig.fetcher.calls) == 2


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_rejected_agent_is_not_asked_again_until_the_rejection_expires(
    status: int,
) -> None:
    rig = _rig()
    rig.fetcher.error = HexgateError("rejected", status=status)
    rig.source.refresh(_SCOPE, _ONE_PATH)

    rig.clock.now += REJECTED_RETRY_SECONDS - 1
    rig.source.refresh(_SCOPE, _TWO_PATHS)
    reading = rig.source.read(_SCOPE, _TWO_PATHS, rig.ledger)
    assert len(rig.fetcher.calls) == 1
    assert reading.state is UsageState.UNAVAILABLE

    rig.clock.now += 1
    rig.source.refresh(_SCOPE, _TWO_PATHS)
    assert len(rig.fetcher.calls) == 2


def test_a_failed_fetch_keeps_last_good_and_reads_stale() -> None:
    rig = _seeded()
    rig.fetcher.error = HexgateError("platform down", status=503)
    rig.clock.now += _SYNC_AFTER

    rig.source.refresh(_SCOPE, _ONE_PATH)
    reading = rig.source.read(_SCOPE, _ONE_PATH, rig.ledger)

    assert len(rig.fetcher.calls) == 2
    assert reading == usage_source.UsageReading({_HOUR: 7}, UsageState.STALE)


def test_a_content_error_keeps_last_good_and_logs_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _seeded()
    rig.fetcher.body = {"values": [1]}
    rig.clock.now += _SYNC_AFTER

    with caplog.at_level(logging.WARNING, logger=usage_source.logger.name):
        rig.source.refresh(_SCOPE, _ONE_PATH)

    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    assert rig.source.read(_SCOPE, _ONE_PATH, rig.ledger).namespace == {_HOUR: 7}


def test_an_unregistered_agent_is_reported_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _rig()
    rig.fetcher.error = HexgateError("not found", status=404)

    with caplog.at_level(logging.WARNING, logger=usage_source.logger.name):
        rig.source.refresh(_SCOPE, _ONE_PATH)
        rig.clock.now += REJECTED_RETRY_SECONDS
        rig.source.refresh(_SCOPE, _ONE_PATH)

    assert len(rig.fetcher.calls) == 2
    assert len(caplog.records) == 1
    assert "not registered" in caplog.records[0].getMessage()


def test_any_fetch_error_is_swallowed() -> None:
    rig = _rig()
    rig.fetcher.error = OSError("boom")

    rig.source.refresh(_SCOPE, _ONE_PATH)

    assert rig.source.read(_SCOPE, _ONE_PATH, rig.ledger).state is (
        UsageState.UNAVAILABLE
    )


def test_the_snapshot_is_stamped_when_the_fetch_starts() -> None:
    rig = _rig(_HeldExecutor())
    rig.fetcher.advance = 1.5
    rig.source.refresh(_SCOPE, _ONE_PATH)

    # Age 60 from the start, 58.5 from the end: only a start stamp reads STALE.
    rig.clock.now = _START + _SYNC_AFTER

    assert rig.source.read(_SCOPE, _ONE_PATH, rig.ledger).state is UsageState.STALE


# ---------------------------------------------------------------------------
# read: states, fail mode, and the gates
# ---------------------------------------------------------------------------


def test_a_fresh_snapshot_adds_the_local_term() -> None:
    rig = _seeded()
    rig.clock.now += 1
    rig.ledger.record({UsageMetric.INVOCATIONS: 2})

    reading = rig.source.read(_SCOPE, _ONE_PATH, rig.ledger)

    assert reading == usage_source.UsageReading({_HOUR: 9}, UsageState.FRESH)


def test_an_event_inside_the_ingest_margin_is_counted_once_more() -> None:
    """G7: recorded just before the fetch and not yet in the platform's value."""
    rig = _rig()
    rig.clock.now -= USAGE_INGEST_MARGIN_SECONDS / 3
    rig.ledger.record({UsageMetric.INVOCATIONS: 1})
    rig.clock.now = _START
    rig.source.refresh(_SCOPE, _ONE_PATH)

    reading = rig.source.read(_SCOPE, _ONE_PATH, rig.ledger)

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

    reading = rig.source.read(_SCOPE, _ONE_PATH, rig.ledger)

    assert reading == usage_source.UsageReading({_HOUR: 2}, UsageState.UNAVAILABLE)


def test_an_unavailable_snapshot_under_deny_reads_nothing() -> None:
    rig = _rig(_HeldExecutor())

    reading = rig.source.read(
        _SCOPE, _ONE_PATH, rig.ledger, on_unavailable=OnUnavailable.DENY
    )

    assert reading == usage_source.UsageReading(None, UsageState.UNAVAILABLE)


def test_a_path_missing_from_the_snapshot_reads_ledger_only() -> None:
    """G4 / F5: a policy adding a path must not deny every run until a refresh."""
    executor = _HeldExecutor()
    rig = _seeded(executor)
    rig.ledger.record({UsageMetric.TOOL_CALLS: 1})

    reading = rig.source.read(_SCOPE, _TWO_PATHS, rig.ledger)

    assert reading == usage_source.UsageReading(
        {_HOUR: 7, _FIVE_MIN: 1}, UsageState.PARTIAL
    )
    executor.run_all()
    assert rig.fetcher.calls[-1][1] == sorted(_TWO_PATHS)


def test_a_path_missing_from_the_snapshot_under_deny_fails_only_itself() -> None:
    rig = _seeded(_HeldExecutor())

    reading = rig.source.read(
        _SCOPE, _TWO_PATHS, rig.ledger, on_unavailable=OnUnavailable.DENY
    )

    assert reading == usage_source.UsageReading({_HOUR: 7}, UsageState.PARTIAL)


def test_a_path_the_platform_left_out_reads_partial() -> None:
    rig = _seeded(_HeldExecutor(), paths=_TWO_PATHS, values={_HOUR: 7})

    reading = rig.source.read(_SCOPE, _TWO_PATHS, rig.ledger)

    assert reading.state is UsageState.PARTIAL
    assert reading.namespace == {_HOUR: 7, _FIVE_MIN: 0}


def test_the_current_run_is_left_out_when_it_is_in_the_local_term() -> None:
    rig = _seeded()
    rig.clock.now += 1
    rig.ledger.record({UsageMetric.INVOCATIONS: 1})

    reading = rig.source.read(_SCOPE, _ONE_PATH, rig.ledger, current_run_age=0.0)

    assert reading.namespace == {_HOUR: 7}


def test_a_mid_run_refresh_holding_the_run_reads_as_at_admission() -> None:
    rig = _seeded()
    rig.clock.now += 1
    run_opened = rig.clock.now
    rig.ledger.record({UsageMetric.INVOCATIONS: 1})
    at_admission = rig.source.read(_SCOPE, _ONE_PATH, rig.ledger, current_run_age=0.0)

    rig.clock.now += _SYNC_AFTER
    rig.fetcher.values[_HOUR] += 1  # the run's own start, now ingested
    rig.source.refresh(_SCOPE, _ONE_PATH)
    after_refresh = rig.source.read(
        _SCOPE, _ONE_PATH, rig.ledger, current_run_age=rig.clock.now - run_opened
    )

    assert after_refresh.namespace == at_admission.namespace == {_HOUR: 7}


def test_read_never_fetches_on_the_callers_thread() -> None:
    executor = _HeldExecutor()
    rig = _rig(executor)

    rig.source.read(_SCOPE, _ONE_PATH, rig.ledger)

    assert rig.fetcher.calls == []
    assert len(executor.jobs) == 1


def test_a_fresh_read_schedules_nothing() -> None:
    executor = _HeldExecutor()
    rig = _seeded(executor)

    rig.source.read(_SCOPE, _ONE_PATH, rig.ledger)

    assert executor.jobs == []


def test_local_reading_is_the_ledger_alone() -> None:
    ledger = new_usage_ledger(clock=_FakeClock())
    ledger.record({UsageMetric.INVOCATIONS: 3})

    assert local_reading(ledger, _ONE_PATH) == usage_source.UsageReading(
        {_HOUR: 3}, UsageState.LOCAL
    )


# ---------------------------------------------------------------------------
# Scope kinds
# ---------------------------------------------------------------------------

_USER_SCOPE = UsageScope(UsageScopeKind.USER, _AGENT)


def test_each_kind_is_fetched_by_its_own_fetcher() -> None:
    rig = _rig(fetch_users=True)

    rig.source.refresh(_SCOPE, _ONE_PATH)
    rig.source.refresh(_USER_SCOPE, _ONE_PATH)

    assert [call[0] for call in rig.fetcher.calls] == [_AGENT]
    assert [call[0] for call in rig.user_fetcher.calls] == [_AGENT]


def test_snapshots_of_two_kinds_with_one_key_never_mix() -> None:
    rig = _rig(fetch_users=True)
    rig.source.refresh(_SCOPE, _ONE_PATH)
    rig.source.refresh(_USER_SCOPE, _ONE_PATH)

    agent = rig.source.read(_SCOPE, _ONE_PATH, rig.ledger)
    user = rig.source.read(_USER_SCOPE, _ONE_PATH, rig.ledger)

    assert agent.namespace == {_HOUR: _PLATFORM_VALUES[_HOUR]}
    assert user.namespace == {_HOUR: _USER_VALUES[_HOUR]}


def test_a_rejected_scope_does_not_throttle_another_kind() -> None:
    rig = _rig(fetch_users=True)
    rig.fetcher.error = HexgateError("rejected", status=403)

    rig.source.refresh(_SCOPE, _ONE_PATH)
    rig.source.refresh(_USER_SCOPE, _ONE_PATH)

    assert len(rig.fetcher.calls) == 1
    assert len(rig.user_fetcher.calls) == 1
    assert rig.source.read(_USER_SCOPE, _ONE_PATH, rig.ledger).state is (
        UsageState.FRESH
    )


@pytest.mark.parametrize("on_unavailable", list(OnUnavailable))
def test_a_kind_without_a_fetcher_never_fetches_and_reads_unavailable(
    on_unavailable: OnUnavailable,
) -> None:
    executor = _HeldExecutor()
    rig = _rig(executor)

    rig.source.refresh(_USER_SCOPE, _ONE_PATH)
    reading = rig.source.read(
        _USER_SCOPE, _ONE_PATH, rig.ledger, on_unavailable=on_unavailable
    )

    assert rig.fetcher.calls == []
    assert executor.jobs == []
    assert reading.state is UsageState.UNAVAILABLE


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

    await rig.source.refresh_async(_SCOPE, _ONE_PATH)
    assert (spy.calls, len(rig.fetcher.calls)) == (1, 1)

    rig.clock.now += _BETWEEN_TIERS
    await rig.source.refresh_async(_SCOPE, _ONE_PATH)
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


def _no_values(_key: str, _paths: Sequence[str]) -> dict[str, Any]:
    return {}


def _client(
    base_url: str = "http://platform",
    *,
    agent: ScopeFetcher = _no_values,
    user: ScopeFetcher = _no_values,
) -> Any:
    return SimpleNamespace(
        config=SimpleNamespace(base_url=base_url),
        get_agent_usage=agent,
        read_user_usage=user,
    )


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


def test_the_shared_source_fetches_agent_scopes_by_bare_name() -> None:
    fetcher = _FakeFetcher(_FakeClock(), _PLATFORM_VALUES)
    source = get_usage_source("k", _client(agent=fetcher.fetch))

    source.refresh(_SCOPE, _ONE_PATH)
    reading = source.read(_SCOPE, _ONE_PATH, new_usage_ledger())

    assert [call[:2] for call in fetcher.calls] == [(_AGENT, [_HOUR])]
    assert reading.state is UsageState.FRESH


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


# ---------------------------------------------------------------------------
# Users: settings
# ---------------------------------------------------------------------------

_USER_MIN_REFRESH = DEFAULT_USER_MIN_REFRESH_SECONDS
_ALICE = "alice@example.com"


def _user(key: str) -> UsageScope:
    return UsageScope.user(key)


def _read_user(rig: _Rig, key: str, **kwargs: Any) -> usage_source.UsageReading:
    return rig.source.read(_user(key), _ONE_PATH, rig.ledger, **kwargs)


def test_user_settings_are_read_from_their_own_variables() -> None:
    environ = {
        "HEXGATE_USAGE_REFRESH_SECONDS": "2",
        "HEXGATE_USAGE_SYNC_AFTER_SECONDS": "30",
        "HEXGATE_USAGE_MAX_STALENESS_SECONDS": "120",
        USER_REFRESH_ENV.min_refresh: "40",
        USER_REFRESH_ENV.sync_after: "90",
        USER_REFRESH_ENV.max_staleness: "400",
    }

    assert UsageRefreshSettings.from_env(environ) == UsageRefreshSettings(
        2.0, 30.0, 120.0
    )
    assert UsageRefreshSettings.from_env(
        environ, env=USER_REFRESH_ENV, defaults=USER_REFRESH_DEFAULTS
    ) == UsageRefreshSettings(40.0, 90.0, 400.0)


def test_user_settings_default_to_30_60_300() -> None:
    expected = UsageRefreshSettings(30.0, 60.0, 300.0)

    assert USER_REFRESH_DEFAULTS == expected
    assert (
        UsageRefreshSettings.from_env(
            {"HEXGATE_USAGE_REFRESH_SECONDS": "2"},
            env=USER_REFRESH_ENV,
            defaults=USER_REFRESH_DEFAULTS,
        )
        == expected
    )


@pytest.mark.parametrize(
    "environ",
    [
        {USER_REFRESH_ENV.min_refresh: "soon"},
        {USER_REFRESH_ENV.sync_after: "900"},
    ],
    ids=["unparsable", "out-of-order"],
)
def test_bad_user_settings_fall_back_to_the_user_defaults(
    environ: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=usage_source.logger.name):
        settings = UsageRefreshSettings.from_env(
            environ, env=USER_REFRESH_ENV, defaults=USER_REFRESH_DEFAULTS
        )

    assert settings == USER_REFRESH_DEFAULTS
    assert len(caplog.records) == 1


def test_a_user_snapshot_younger_than_min_refresh_schedules_nothing() -> None:
    executor = _HeldExecutor()
    rig = _user_rig(executor)
    rig.source.refresh(_user("a"), _ONE_PATH)

    rig.clock.now += _BETWEEN_TIERS
    rig.source.refresh(_user("a"), _ONE_PATH)
    assert executor.jobs == []

    rig.clock.now = _START + _USER_MIN_REFRESH
    rig.source.refresh(_user("a"), _ONE_PATH)
    assert len(executor.jobs) == 1


# ---------------------------------------------------------------------------
# Users: the LRU (F5)
# ---------------------------------------------------------------------------


def test_the_least_recently_used_user_is_evicted_past_capacity() -> None:
    rig = _user_rig(_HeldExecutor())
    for key in ("a", "b", "c"):
        rig.source.refresh(_user(key), _ONE_PATH)

    assert _read_user(rig, "b").state is UsageState.FRESH
    assert _read_user(rig, "c").state is UsageState.FRESH
    assert _read_user(rig, "a").state is UsageState.UNAVAILABLE
    rig.source.refresh(_user("a"), _ONE_PATH)

    assert [call[0] for call in rig.user_fetcher.calls] == ["a", "b", "c", "a"]
    assert rig.user_fetcher.calls[-1][2] == threading.current_thread().name


def test_reading_a_user_keeps_it_from_eviction() -> None:
    rig = _user_rig(_HeldExecutor())
    rig.source.refresh(_user("a"), _ONE_PATH)
    rig.source.refresh(_user("b"), _ONE_PATH)

    _read_user(rig, "a")
    rig.source.refresh(_user("c"), _ONE_PATH)

    assert _read_user(rig, "a").state is UsageState.FRESH
    assert _read_user(rig, "b").state is UsageState.UNAVAILABLE


def test_eviction_drops_a_users_rejection_and_throttle() -> None:
    """F5: the side state goes with the snapshot."""
    rig = _user_rig()
    rig.user_fetcher.error = HexgateError("refused", status=422)
    rig.source.refresh(_user("a"), _ONE_PATH)
    rig.user_fetcher.error = None
    rig.source.refresh(_user("a"), _ONE_PATH)
    assert len(rig.user_fetcher.calls) == 1

    rig.source.refresh(_user("b"), _ONE_PATH)
    rig.source.refresh(_user("c"), _ONE_PATH)
    rig.source.refresh(_user("a"), _ONE_PATH)

    assert [call[0] for call in rig.user_fetcher.calls] == ["a", "b", "c", "a"]


def test_the_user_table_never_outgrows_its_capacity() -> None:
    rig = _user_rig()

    for index in range(1_000):
        rig.source.refresh(_user(f"user-{index}"), _ONE_PATH)

    assert len(rig.source._tables[UsageScopeKind.USER]) == 2


def test_agents_are_never_evicted() -> None:
    rig = _rig(
        fetch_users=True,
        user_spec=lambda fetch: ScopeKindSpec(fetch, _SETTINGS, capacity=1),
    )
    agents = [UsageScope.agent(f"agent-{index}") for index in range(50)]

    for scope in agents:
        rig.source.refresh(scope, _ONE_PATH)
    rig.source.refresh(_user("a"), _ONE_PATH)
    rig.source.refresh(_user("b"), _ONE_PATH)

    assert {
        rig.source.read(scope, _ONE_PATH, rig.ledger).state for scope in agents
    } == {UsageState.FRESH}
    assert len(rig.fetcher.calls) == len(agents)


# ---------------------------------------------------------------------------
# Users: the shared back-off (F6)
# ---------------------------------------------------------------------------


def test_a_failed_user_fetch_pauses_every_user_for_min_refresh() -> None:
    executor = _HeldExecutor()
    rig = _user_rig(executor)
    rig.user_fetcher.error = HexgateError("unavailable", status=503)
    rig.source.refresh(_user("a"), _ONE_PATH)
    rig.user_fetcher.error = None
    rig.ledger.record({UsageMetric.INVOCATIONS: 1})

    rig.clock.now += _USER_MIN_REFRESH - 1
    rig.source.refresh(_user("b"), _ONE_PATH)
    denied = _read_user(rig, "b", on_unavailable=OnUnavailable.DENY)
    allowed = _read_user(rig, "b")

    assert len(rig.user_fetcher.calls) == 1
    assert executor.jobs == []
    assert denied == usage_source.UsageReading(None, UsageState.UNAVAILABLE)
    assert allowed == usage_source.UsageReading({_HOUR: 1}, UsageState.UNAVAILABLE)

    rig.clock.now += 1
    rig.source.refresh(_user("b"), _ONE_PATH)
    assert [call[0] for call in rig.user_fetcher.calls] == ["a", "b"]


async def test_a_paused_cold_user_never_leaves_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _user_rig()
    rig.user_fetcher.error = HexgateError("unavailable", status=503)
    rig.source.refresh(_user("a"), _ONE_PATH)
    spy = _ToThreadSpy()
    monkeypatch.setattr(asyncio, "to_thread", spy)

    await rig.source.refresh_async(_user("b"), _ONE_PATH)

    assert spy.calls == 0
    assert len(rig.user_fetcher.calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_a_401_or_403_pauses_every_user_for_the_rejection_window(
    status: int,
) -> None:
    rig = _user_rig()
    rig.user_fetcher.error = HexgateError("rejected", status=status)
    rig.source.refresh(_user("a"), _ONE_PATH)
    rig.user_fetcher.error = None

    rig.clock.now = _START + REJECTED_RETRY_SECONDS - 1
    rig.source.refresh(_user("b"), _ONE_PATH)
    assert len(rig.user_fetcher.calls) == 1

    rig.clock.now += 1
    rig.source.refresh(_user("b"), _ONE_PATH)
    assert len(rig.user_fetcher.calls) == 2


def test_a_404_on_users_reports_a_missing_route_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _user_rig()
    rig.user_fetcher.error = HexgateError("not found", status=404)

    with caplog.at_level(logging.WARNING, logger=usage_source.logger.name):
        rig.source.refresh(_user("a"), _ONE_PATH)
        rig.source.refresh(_user("b"), _ONE_PATH)
        rig.clock.now += REJECTED_RETRY_SECONDS
        rig.source.refresh(_user("c"), _ONE_PATH)
        rig.source.refresh(_user("d"), _ONE_PATH)

    assert [call[0] for call in rig.user_fetcher.calls] == ["a", "c"]
    assert len(caplog.records) == 1
    assert "no user usage read" in caplog.records[0].getMessage()


def test_a_422_rejects_only_that_user() -> None:
    rig = _user_rig()
    rig.user_fetcher.error = HexgateError("refused", status=422)
    rig.source.refresh(_user("a"), _ONE_PATH)
    rig.user_fetcher.error = None

    rig.source.refresh(_user("b"), _ONE_PATH)
    rig.clock.now += REJECTED_RETRY_SECONDS - 1
    rig.source.refresh(_user("a"), _ONE_PATH)

    assert [call[0] for call in rig.user_fetcher.calls] == ["a", "b"]

    rig.clock.now += 1
    rig.source.refresh(_user("a"), _ONE_PATH)
    assert [call[0] for call in rig.user_fetcher.calls] == ["a", "b", "a"]


def test_a_content_error_pauses_users() -> None:
    rig = _user_rig()
    rig.user_fetcher.body = {"values": [1]}
    rig.source.refresh(_user("a"), _ONE_PATH)
    rig.user_fetcher.body = None

    rig.source.refresh(_user("b"), _ONE_PATH)
    assert len(rig.user_fetcher.calls) == 1

    rig.clock.now += _USER_MIN_REFRESH
    rig.source.refresh(_user("b"), _ONE_PATH)
    assert len(rig.user_fetcher.calls) == 2


def test_a_pause_logs_once_however_many_users_wait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _user_rig()
    rig.user_fetcher.error = HexgateError("unavailable", status=503)
    # b's fetch starts and fails while a's is still running.
    rig.user_fetcher.during = lambda _key: rig.source.refresh(_user("b"), _ONE_PATH)

    with caplog.at_level(logging.WARNING, logger=usage_source.logger.name):
        rig.source.refresh(_user("a"), _ONE_PATH)
        for key in ("c", "d", "e"):
            rig.source.refresh(_user(key), _ONE_PATH)

    assert [call[0] for call in rig.user_fetcher.calls] == ["a", "b"]
    assert len(caplog.records) == 1


def test_a_user_failure_does_not_pause_agents() -> None:
    rig = _user_rig()
    rig.user_fetcher.error = HexgateError("unavailable", status=503)
    rig.source.refresh(_user("a"), _ONE_PATH)

    rig.source.refresh(_SCOPE, _ONE_PATH)

    assert len(rig.fetcher.calls) == 1


def test_an_agent_failure_does_not_pause_users() -> None:
    rig = _user_rig()
    rig.fetcher.error = HexgateError("unavailable", status=503)
    rig.source.refresh(_SCOPE, _ONE_PATH)

    rig.source.refresh(_user("a"), _ONE_PATH)

    assert len(rig.user_fetcher.calls) == 1


def test_an_agent_failure_still_backs_off_that_agent_only() -> None:
    rig = _user_rig()
    other = UsageScope.agent("support")
    rig.fetcher.error = HexgateError("unavailable", status=503)
    rig.source.refresh(_SCOPE, _ONE_PATH)
    rig.fetcher.error = None

    rig.source.refresh(other, _ONE_PATH)
    rig.source.refresh(_SCOPE, _ONE_PATH)

    assert [call[0] for call in rig.fetcher.calls] == [_AGENT, "support"]


# ---------------------------------------------------------------------------
# Users: the ID pre-check and G10
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["x" * (USER_ID_MAX_LENGTH + 1), "\ud800"],
    ids=["too-long", "lone-surrogate"],
)
def test_an_unreadable_user_id_is_never_sent(
    key: str, caplog: pytest.LogCaptureFixture
) -> None:
    executor = _HeldExecutor()
    rig = _user_rig(executor)

    with caplog.at_level(logging.WARNING, logger=usage_source.logger.name):
        rig.source.refresh(_user(key), _ONE_PATH)
        reading = _read_user(rig, key)
        rig.source.refresh(_user(key), _ONE_PATH)
        rig.source.refresh(_user("a"), _ONE_PATH)

    assert reading.state is UsageState.UNAVAILABLE
    assert executor.jobs == []
    assert [call[0] for call in rig.user_fetcher.calls] == ["a"]
    assert len(rig.source._tables[UsageScopeKind.USER]) == 1
    assert len(caplog.records) == 1


def test_a_256_character_multibyte_user_id_is_sent() -> None:
    key = "é" * USER_ID_MAX_LENGTH
    rig = _user_rig()

    rig.source.refresh(_user(key), _ONE_PATH)

    assert len(key.encode()) > USER_ID_MAX_LENGTH
    assert [call[0] for call in rig.user_fetcher.calls] == [key]


def _fail_with_status(status: int) -> Callable[[_Rig], None]:
    def arrange(rig: _Rig) -> None:
        rig.user_fetcher.error = HexgateError(f"{status} for {_ALICE}", status=status)

    return arrange


def _fail_with_body(rig: _Rig) -> None:
    rig.user_fetcher.body = {"values": {_HOUR: _ALICE}}


def _fail_in_transport(rig: _Rig) -> None:
    rig.user_fetcher.error = OSError(f"connection reset reading {_ALICE}")


@pytest.mark.parametrize(
    ("arrange", "key"),
    [
        (_fail_with_status(503), _ALICE),
        (_fail_with_status(401), _ALICE),
        (_fail_with_status(404), _ALICE),
        (_fail_with_status(422), _ALICE),
        (_fail_with_body, _ALICE),
        (_fail_in_transport, _ALICE),
        (lambda _rig: None, _ALICE + "x" * USER_ID_MAX_LENGTH),
    ],
    ids=["503", "401", "404", "422", "content", "transport", "unreadable"],
)
def test_no_log_line_carries_a_user_id(
    arrange: Callable[[_Rig], None], key: str, caplog: pytest.LogCaptureFixture
) -> None:
    """G10: an ID in a log line can't be recalled."""
    rig = _user_rig()
    arrange(rig)

    with caplog.at_level(logging.DEBUG, logger=usage_source.logger.name):
        rig.source.refresh(_user(key), _ONE_PATH)
        _read_user(rig, key)

    assert caplog.records
    assert "alice" not in caplog.text


# ---------------------------------------------------------------------------
# Users: the shared source
# ---------------------------------------------------------------------------


def test_the_shared_source_reads_users_through_the_client() -> None:
    clock = _FakeClock()
    agent, user = _FakeFetcher(clock, _PLATFORM_VALUES), _FakeFetcher(clock, {})
    source = get_usage_source("k", _client(agent=agent.fetch, user=user.fetch))

    source.refresh(_user(_ALICE), _TWO_PATHS)
    source.refresh(_SCOPE, _ONE_PATH)

    assert [call[:2] for call in user.calls] == [(_ALICE, sorted(_TWO_PATHS))]
    assert [call[:2] for call in agent.calls] == [(_AGENT, [_HOUR])]


def test_the_shared_source_sizes_users_from_the_user_cache_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(USER_CACHE_ENV, "1")
    clock = _FakeClock()
    agent, user = _FakeFetcher(clock, {}), _FakeFetcher(clock, _USER_VALUES)
    source = get_usage_source("k", _client(agent=agent.fetch, user=user.fetch))

    for key in ("a", "b", "a"):
        source.refresh(_user(key), _ONE_PATH)

    assert [call[0] for call in user.calls] == ["a", "b", "a"]


def test_agent_scopes_alone_never_touch_the_user_kind() -> None:
    """G3 for this PR: nothing about users runs until a user scope is asked for."""
    clock = _FakeClock()
    agent, user = _FakeFetcher(clock, _PLATFORM_VALUES), _FakeFetcher(clock, {})
    source = get_usage_source("k", _client(agent=agent.fetch, user=user.fetch))

    source.refresh(_SCOPE, _ONE_PATH)
    source.read(_SCOPE, _ONE_PATH, new_usage_ledger())

    assert user.calls == []
    assert len(source._tables[UsageScopeKind.USER]) == 0
