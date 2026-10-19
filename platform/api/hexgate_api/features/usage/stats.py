"""Per-scope usage read stats: rate, memo hit rate, latency, one log line a minute."""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

USAGE_LOGGER: Final = "hexgate_api.features.usage"
_LOG_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"
USAGE_STATS_INTERVAL_SECONDS: Final = 60.0
# 01 §4.8 guardrail 2: past this, start the near-limit watchlist (README DP3).
USER_READ_ALERT_PER_SECOND: Final = 200.0
USAGE_STATS_MAX_SAMPLES: Final = 65_536
AGENT_SCOPE: Final = "agent"
USER_SCOPE: Final = "user"
_MS_PER_SECOND: Final = 1_000
_PERCENT: Final = 100
_P50: Final = 50
_P99: Final = 99
_SUMMARY_FORMAT: Final = (
    "usage reads [%s]: %d in %.0fs (%.1f/s), %d queries (memo hit %.0f%%), "
    "%d failed, p50 %.0f ms, p99 %.0f ms"
)
_ALERT_SUFFIX: Final = " — above the %.0f/s alert: start the near-limit watchlist"

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UsageReadSummary:
    scope: str
    requests: int
    queries: int
    failures: int
    elapsed_seconds: float
    p50_ms: float
    p99_ms: float

    @property
    def per_second(self) -> float:
        return self.requests / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0

    @property
    def memo_hit_ratio(self) -> float:
        return 1 - self.queries / self.requests if self.requests else 0.0


class UsageReadStats:
    """One scope's reads since the last summary. Event-loop only, like UsageMemo.
    Never sees the key being read: it can't log a user_id (README G10)."""

    def __init__(
        self,
        scope: str,
        *,
        interval_seconds: float,
        alert_per_second: float | None,
        max_samples: int,
        clock: Callable[[], float],
        logger: logging.Logger,
    ) -> None:
        self._scope = scope
        self._interval = interval_seconds
        self._alert = alert_per_second
        self._clock = clock
        self._logger = logger
        self._latencies: deque[float] = deque(maxlen=max_samples)
        self._started: float | None = None
        self._requests = 0
        self._queries = 0
        self._failures = 0

    def record(self, *, latency_seconds: float, queried: bool, failed: bool) -> None:
        """Count one request, first logging and resetting an expired window.

        Flushing before counting starts a burst after idle time in a window of
        its own, so the idle time cannot dilute it below the alert."""
        now = self._clock()
        if self._started is not None and now - self._started >= self._interval:
            self._flush()
        if self._started is None:
            self._started = now
        self._requests += 1
        self._queries += queried
        self._failures += failed
        self._latencies.append(latency_seconds)

    def summary(self) -> UsageReadSummary:
        # Every counted read fell inside one interval, so a late flush rates them
        # over the interval, not over the idle time that followed.
        elapsed = (
            0.0
            if self._started is None
            else min(self._clock() - self._started, self._interval)
        )
        latencies = sorted(self._latencies)
        return UsageReadSummary(
            scope=self._scope,
            requests=self._requests,
            queries=self._queries,
            failures=self._failures,
            elapsed_seconds=elapsed,
            p50_ms=_nearest_rank(latencies, _P50) * _MS_PER_SECOND,
            p99_ms=_nearest_rank(latencies, _P99) * _MS_PER_SECOND,
        )

    def _flush(self) -> None:
        summary = self.summary()
        args = (
            summary.scope,
            summary.requests,
            summary.elapsed_seconds,
            summary.per_second,
            summary.queries,
            summary.memo_hit_ratio * _PERCENT,
            summary.failures,
            summary.p50_ms,
            summary.p99_ms,
        )
        if self._alert is not None and summary.per_second > self._alert:
            self._logger.warning(_SUMMARY_FORMAT + _ALERT_SUFFIX, *args, self._alert)
        else:
            self._logger.info(_SUMMARY_FORMAT, *args)
        self._latencies.clear()
        self._started = None
        self._requests = self._queries = self._failures = 0


def configure_usage_logging() -> None:
    """uvicorn configures only its own loggers, so hexgate_api INFO records are
    dropped by the root's last-resort handler. The usage read stats must reach
    the log (DP2 reads them)."""
    logger = logging.getLogger(USAGE_LOGGER)
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _nearest_rank(ordered: Sequence[float], percentile: int) -> float:
    if not ordered:
        return 0.0
    rank = math.ceil(percentile / _PERCENT * len(ordered))
    return ordered[rank - 1]


@lru_cache
def get_agent_read_stats() -> UsageReadStats:
    return UsageReadStats(
        AGENT_SCOPE,
        interval_seconds=USAGE_STATS_INTERVAL_SECONDS,
        alert_per_second=None,
        max_samples=USAGE_STATS_MAX_SAMPLES,
        clock=time.monotonic,
        logger=_log,
    )


@lru_cache
def get_user_read_stats() -> UsageReadStats:
    return UsageReadStats(
        USER_SCOPE,
        interval_seconds=USAGE_STATS_INTERVAL_SECONDS,
        alert_per_second=USER_READ_ALERT_PER_SECOND,
        max_samples=USAGE_STATS_MAX_SAMPLES,
        clock=time.monotonic,
        logger=_log,
    )
