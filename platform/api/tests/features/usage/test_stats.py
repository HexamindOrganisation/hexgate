"""Per-scope usage read stats, and the handler that makes their INFO line visible."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from hexgate_api import main
from hexgate_api.features.usage.stats import (
    USER_READ_ALERT_PER_SECOND,
    UsageReadStats,
    get_agent_read_stats,
    get_user_read_stats,
)

_INTERVAL = 60.0
_ALERT = 2.0
_LOGGER = "tests.usage.stats"
_USAGE_STATS_LOGGER = "hexgate_api.features.usage.stats"


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _stats(
    clock: _FakeClock, *, alert: float | None = _ALERT, max_samples: int = 1_000
) -> UsageReadStats:
    return UsageReadStats(
        "user",
        interval_seconds=_INTERVAL,
        alert_per_second=alert,
        max_samples=max_samples,
        clock=clock,
        logger=logging.getLogger(_LOGGER),
    )


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER]


def test_nothing_is_logged_before_the_interval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _FakeClock()
    stats = _stats(clock)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        for _ in range(3):
            stats.record(latency_seconds=0.01, queried=True, failed=False)
        clock.now = _INTERVAL - 1
        stats.record(latency_seconds=0.01, queried=False, failed=False)

    assert _records(caplog) == []
    assert stats.summary().requests == 4


def test_the_interval_logs_one_summary_and_resets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _FakeClock()
    stats = _stats(clock, alert=None)
    # 100 samples of 1..100 ms: nearest-rank p50 is 50 ms, p99 is 99 ms.
    latencies = [ms / 1_000 for ms in range(1, 101)]

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        for i, latency in enumerate(latencies[:-1]):
            stats.record(latency_seconds=latency, queried=i < 25, failed=i == 0)
        before = stats.summary()
        clock.now = _INTERVAL
        stats.record(latency_seconds=latencies[-1], queried=False, failed=False)

    assert before.requests == 99
    assert before.queries == 25
    assert before.failures == 1
    (record,) = _records(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        "usage reads [user]: 100 in 60s (1.7/s), 25 queries (memo hit 75%), "
        "1 failed, p50 50 ms, p99 99 ms"
    )
    assert stats.summary().requests == 0


def test_the_summary_figures(caplog: pytest.LogCaptureFixture) -> None:
    clock = _FakeClock()
    stats = _stats(clock)
    for latency in (0.004, 0.002, 0.010, 0.001):
        stats.record(latency_seconds=latency, queried=True, failed=False)
    stats.record(latency_seconds=0.003, queried=False, failed=False)
    clock.now = 10.0

    summary = stats.summary()

    assert summary.elapsed_seconds == 10.0
    assert summary.per_second == 0.5
    assert summary.memo_hit_ratio == pytest.approx(0.2)
    assert summary.p50_ms == pytest.approx(3.0)
    assert summary.p99_ms == pytest.approx(10.0)


def test_above_the_alert_the_summary_is_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _FakeClock()
    stats = _stats(clock)
    requests = int(_ALERT * _INTERVAL) + 1

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        for _ in range(requests - 1):
            stats.record(latency_seconds=0.001, queried=True, failed=False)
        clock.now = _INTERVAL
        stats.record(latency_seconds=0.001, queried=True, failed=False)

    (record,) = _records(caplog)
    assert record.levelno == logging.WARNING
    assert "above the 2/s alert: start the near-limit watchlist" in record.getMessage()


def test_at_the_alert_the_summary_stays_info(caplog: pytest.LogCaptureFixture) -> None:
    clock = _FakeClock()
    stats = _stats(clock)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        for _ in range(int(_ALERT * _INTERVAL) - 1):
            stats.record(latency_seconds=0.001, queried=True, failed=False)
        clock.now = _INTERVAL
        stats.record(latency_seconds=0.001, queried=True, failed=False)

    (record,) = _records(caplog)
    assert record.levelno == logging.INFO


def test_no_alert_threshold_never_warns(caplog: pytest.LogCaptureFixture) -> None:
    clock = _FakeClock()
    stats = _stats(clock, alert=None)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        for _ in range(10_000):
            stats.record(latency_seconds=0.001, queried=True, failed=False)
        clock.now = _INTERVAL
        stats.record(latency_seconds=0.001, queried=True, failed=False)

    (record,) = _records(caplog)
    assert record.levelno == logging.INFO


def test_max_samples_bounds_the_latencies_kept() -> None:
    clock = _FakeClock()
    stats = _stats(clock, max_samples=3)
    for latency in (1.0, 1.0, 1.0, 0.001, 0.002, 0.003):
        stats.record(latency_seconds=latency, queried=True, failed=False)

    summary = stats.summary()

    assert summary.requests == 6
    assert summary.p99_ms == pytest.approx(3.0)


def test_no_requests_reads_zeros() -> None:
    summary = _stats(_FakeClock()).summary()

    assert summary.requests == 0
    assert summary.memo_hit_ratio == 0.0
    assert summary.per_second == 0.0
    assert summary.p50_ms == summary.p99_ms == 0.0


def test_only_user_reads_carry_the_alert() -> None:
    assert get_user_read_stats()._alert == USER_READ_ALERT_PER_SECOND
    assert get_agent_read_stats()._alert is None


@pytest.fixture
def usage_logger() -> Iterator[logging.Logger]:
    logger = logging.getLogger(main._USAGE_LOGGER)
    saved_handlers, saved_level = logger.handlers[:], logger.level
    logger.handlers.clear()
    try:
        yield logger
    finally:
        logger.handlers[:] = saved_handlers
        logger.setLevel(saved_level)


def test_usage_logging_is_configured_once_and_passes_info(
    usage_logger: logging.Logger,
) -> None:
    main._configure_usage_logging()
    main._configure_usage_logging()

    (handler,) = usage_logger.handlers
    seen: list[logging.LogRecord] = []
    handler.emit = seen.append  # type: ignore[method-assign]
    logging.getLogger(_USAGE_STATS_LOGGER).info("usage reads [user]: …")

    assert [r.getMessage() for r in seen] == ["usage reads [user]: …"]
