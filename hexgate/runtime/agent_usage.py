"""Process-local usage per agent — the local term of ``agent_usage.*``.

Counts what *this process* did for each agent, in time buckets, so a policy can read
"invocations in the last hour" without a network call. Recorded from ``RunFacts``, so
each metric is counted on the same line as its ``run.*`` twin.

Inert until :meth:`UsageLedgers.enable`: a process whose policy references no
``agent_usage.*`` path never creates a ledger.
"""

from __future__ import annotations

import threading
import time
from array import array
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

Clock = Callable[[], float]


class UsageMetric(StrEnum):
    """What the ledger records: the ``run.*`` names plus ``invocations``.
    ``total_tokens`` is derived when a namespace is built, never recorded."""

    INVOCATIONS = "invocations"
    TOOL_CALLS = "tool_calls"
    DENIALS = "denials"
    LLM_CALLS = "llm_calls"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"


_SECONDS_PER_MINUTE: Final = 60
_SECONDS_PER_HOUR: Final = 3_600
_SECONDS_PER_DAY: Final = 86_400
# The longest window ``agent_usage.*`` may register. A longer one would silently read
# a truncated count, so the registry's windows are pinned against this.
MAX_WINDOW_SECONDS: Final[float] = 30 * _SECONDS_PER_DAY
# Three tiers, finest first. Fine buckets serve the snapshot cut-off, minutes old at
# most; minute buckets serve windows up to a day; hour buckets serve the rest, so a
# 7- or 30-day window over-counts by under an hour — the conservative direction.
FINE_BUCKET_SECONDS: Final[float] = 1.0
FINE_RETENTION_SECONDS: Final[float] = 300.0
MINUTE_BUCKET_SECONDS: Final[float] = float(_SECONDS_PER_MINUTE)
MINUTE_RETENTION_SECONDS: Final[float] = float(_SECONDS_PER_DAY)
HOUR_BUCKET_SECONDS: Final[float] = float(_SECONDS_PER_HOUR)

# Rolling windows only. Each path is <metric>_<window>. The platform endpoint accepts a
# pattern, so adding a window later is an SDK-only change, as long as it stays
# <= MAX_WINDOW_SECONDS.
USAGE_WINDOWS: Final[Mapping[str, float]] = MappingProxyType(
    {
        "5m": 5 * _SECONDS_PER_MINUTE,
        "1h": _SECONDS_PER_HOUR,
        "24h": _SECONDS_PER_DAY,
        "7d": 7 * _SECONDS_PER_DAY,
        "30d": 30 * _SECONDS_PER_DAY,
    }
)
# Derived when a namespace is built, never recorded: input + output.
TOTAL_TOKENS: Final = "total_tokens"
USAGE_METRICS: Final[tuple[str, ...]] = (
    *(metric.value for metric in UsageMetric),
    TOTAL_TOKENS,
)


@dataclass(frozen=True, slots=True)
class UsagePath:
    metric: str
    window_seconds: float


# Every agent_usage.* path a policy may reference: 7 metrics x 5 windows = 35.
AGENT_USAGE_PATHS: Final[Mapping[str, UsagePath]] = MappingProxyType(
    {
        f"{metric}_{window}": UsagePath(metric, seconds)
        for metric in USAGE_METRICS
        for window, seconds in USAGE_WINDOWS.items()
    }
)
KNOWN_AGENT_USAGE_PATHS: Final[frozenset[str]] = frozenset(AGENT_USAGE_PATHS)
# How errors name the registry: the rule reads better than the 35 names it generates.
AGENT_USAGE_VOCABULARY: Final = (
    f"<metric>_<window>, metrics: {', '.join(sorted(USAGE_METRICS))}; "
    f"windows: {', '.join(USAGE_WINDOWS)}"
)

# One signed 64-bit counter per metric per bucket.
_ZERO_COUNTER: Final = array("q", [0])


class BucketSeries:
    """Counts in a fixed ring of ``width``-second buckets spanning ``retention``.

    Memory is fixed at construction: one counter per metric per bucket. A slot is
    zeroed when the ring laps it, so a stale count is never read.

    Not thread-safe: :class:`UsageLedger` serialises access.
    """

    def __init__(self, width: float, retention: float) -> None:
        self._width = width
        self.retention = retention
        # +1 so a cut-off exactly ``retention`` old still finds the bucket holding it.
        self._slots = int(retention // width) + 1
        self._counts = {metric: _ZERO_COUNTER * self._slots for metric in UsageMetric}
        self._newest: int | None = None

    def add(self, now: float, amounts: Mapping[UsageMetric, int]) -> None:
        """A clock step backwards counts in the newest bucket, which stays in a
        window longest: an over-count, never a lost one."""
        slot = self._advance(self._index(now)) % self._slots
        for metric, amount in amounts.items():
            self._counts[metric][slot] += amount

    def total_since(self, instant: float) -> dict[UsageMetric, int]:
        """Every bucket from the one holding ``instant`` onwards. That bucket counts
        whole, an over-count of under one width — the conservative direction."""
        totals = dict.fromkeys(UsageMetric, 0)
        if self._newest is None:
            return totals
        first = max(self._index(instant), self._newest - self._slots + 1)
        spans = self._spans(first, self._newest - first + 1)
        for metric, counts in self._counts.items():
            totals[metric] = sum(sum(counts[start:end]) for start, end in spans)
        return totals

    def _advance(self, index: int) -> int:
        """Make ``index`` the newest bucket, zeroing the slots it laps; returns the
        bucket to add into."""
        if self._newest is None:
            self._newest = index
        elif index > self._newest:
            lapped = min(index - self._newest, self._slots)
            for start, end in self._spans(index - lapped + 1, lapped):
                for counts in self._counts.values():
                    counts[start:end] = _ZERO_COUNTER * (end - start)
            self._newest = index
        return self._newest

    def _spans(self, first: int, count: int) -> tuple[tuple[int, int], ...]:
        """The slot ranges holding ``count`` buckets from ``first``: one, or two
        where the ring wraps."""
        if count <= 0:
            return ()
        start = first % self._slots
        end = start + count
        if end <= self._slots:
            return ((start, end),)
        return ((start, self._slots), (0, end - self._slots))

    def _index(self, instant: float) -> int:
        return int(instant // self._width)


class UsageLedger:
    """One agent's usage in this process. Reads return every :class:`UsageMetric`,
    zero when unused, so a reader never meets a missing key."""

    def __init__(self, series: Sequence[BucketSeries], clock: Clock) -> None:
        """``series`` is finest first; a read uses the finest one whose retention
        covers its span."""
        self._series = tuple(series)
        self._clock = clock
        self._lock = threading.Lock()

    def record(self, amounts: Mapping[UsageMetric, int]) -> None:
        """Add ``amounts`` atomically, so an LLM call is never seen without its
        tokens. Non-positive amounts are dropped: the ledger is monotone. ``run.*``
        sums them as given, so the two agree only while providers report
        non-negative counts, as every current adapter does."""
        positive = {metric: amount for metric, amount in amounts.items() if amount > 0}
        if not positive:
            return
        with self._lock:
            now = self._clock()
            for series in self._series:
                series.add(now, positive)

    def since(self, instant: float) -> dict[UsageMetric, int]:
        """Usage from the monotonic ``instant`` to now."""
        with self._lock:
            return self._covering(self._clock() - instant).total_since(instant)

    def within(self, seconds: float) -> dict[UsageMetric, int]:
        """Usage over the trailing ``seconds``, a rolling window."""
        # One clock read, and the tier chosen by the span itself: a second read
        # would push a tier-retention window just past it, onto the next tier.
        with self._lock:
            return self._covering(seconds).total_since(self._clock() - seconds)

    def _covering(self, span: float) -> BucketSeries:
        return next(
            (series for series in self._series if span <= series.retention),
            self._series[-1],
        )


def _monotonic() -> float:
    # Resolved per call, like ``RunFacts._started_monotonic``: a bound reference
    # would ignore a patched clock that ``run.elapsed_seconds`` follows.
    return time.monotonic()


def new_usage_ledger(clock: Clock = _monotonic) -> UsageLedger:
    return UsageLedger(
        series=(
            BucketSeries(FINE_BUCKET_SECONDS, FINE_RETENTION_SECONDS),
            BucketSeries(MINUTE_BUCKET_SECONDS, MINUTE_RETENTION_SECONDS),
            BucketSeries(HOUR_BUCKET_SECONDS, MAX_WINDOW_SECONDS),
        ),
        clock=clock,
    )


def ledger_namespace(
    ledger: UsageLedger,
    paths: Iterable[str],
    *,
    current_run_age: float | None = None,
) -> dict[str, int]:
    """The ``agent_usage`` mapping for ``paths``, from this process's ledger alone.

    One ledger read per distinct window, not per path. Unknown paths raise
    ``KeyError``: callers pass only registered paths.

    ``current_run_age`` is the age of the agent's run being decided inside, if any.
    ``invocations_*`` then leaves that run out, so a cap reads the same inside the
    run as it did at admission, before the run was counted.
    """
    wanted = [(path, AGENT_USAGE_PATHS[path]) for path in paths]
    windows = {spec.window_seconds for _, spec in wanted}
    reads = {seconds: ledger.within(seconds) for seconds in windows}
    # Floored: a run opened before the ledger was enabled never recorded itself.
    return {
        path: max(
            _metric_value(reads[spec.window_seconds], spec.metric)
            - _own_invocation(spec, current_run_age),
            0,
        )
        for path, spec in wanted
    }


def _own_invocation(spec: UsagePath, current_run_age: float | None) -> int:
    # Only while the run's invocation can still be in the window: once it has aged
    # out, subtracting would hide another run's. Bucket round-up may keep it counted
    # a little longer, which reads one high, the strict direction.
    if current_run_age is None or spec.metric != UsageMetric.INVOCATIONS:
        return 0
    return 1 if current_run_age < spec.window_seconds else 0


def _metric_value(totals: Mapping[UsageMetric, int], metric: str) -> int:
    if metric == TOTAL_TOKENS:
        return totals[UsageMetric.INPUT_TOKENS] + totals[UsageMetric.OUTPUT_TOKENS]
    return totals[UsageMetric(metric)]


class UsageLedgers:
    """The process's ledgers by agent name. Off until :meth:`enable`, then on for
    good, so a later policy re-adding a usage path finds its history intact."""

    def __init__(self, ledger_factory: Callable[[], UsageLedger]) -> None:
        self._ledger_factory = ledger_factory
        self._enabled = False
        self._ledgers: dict[str, UsageLedger] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        self._enabled = True

    def ledger_for(self, agent: str) -> UsageLedger | None:
        """``agent``'s ledger, created on first use; ``None`` while disabled."""
        if not self._enabled:
            return None
        with self._lock:
            ledger = self._ledgers.get(agent)
            if ledger is None:
                ledger = self._ledgers[agent] = self._ledger_factory()
            return ledger


AGENT_USAGE_LEDGERS: Final[UsageLedgers] = UsageLedgers(new_usage_ledger)
