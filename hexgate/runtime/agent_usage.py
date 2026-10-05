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
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
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


_SECONDS_PER_DAY: Final = 86_400
# The longest window ``agent_usage.*`` may register. A longer one would silently read
# a truncated count, so 2b pins its windows against this.
MAX_WINDOW_SECONDS: Final[float] = 30 * _SECONDS_PER_DAY
# Fine buckets serve the snapshot cut-off, minutes old at most; coarse ones serve the
# long windows in bounded memory.
FINE_BUCKET_SECONDS: Final[float] = 1.0
FINE_RETENTION_SECONDS: Final[float] = 300.0
COARSE_BUCKET_SECONDS: Final[float] = 60.0


@dataclass(slots=True)
class _Bucket:
    index: int
    counts: dict[UsageMetric, int] = field(default_factory=dict)


class BucketSeries:
    """Counts in fixed-width time buckets, oldest first, pruned past ``retention``.

    Not thread-safe: :class:`UsageLedger` serialises access.
    """

    def __init__(self, width: float, retention: float) -> None:
        self._width = width
        self.retention = retention
        self._buckets: deque[_Bucket] = deque()

    def add(self, now: float, amounts: Mapping[UsageMetric, int]) -> None:
        index = self._index(now)
        if not self._buckets or self._buckets[-1].index != index:
            self._buckets.append(_Bucket(index))
        counts = self._buckets[-1].counts
        for metric, amount in amounts.items():
            counts[metric] = counts.get(metric, 0) + amount
        self._prune(now)

    def total_since(self, instant: float) -> dict[UsageMetric, int]:
        """Every bucket from the one holding ``instant`` onwards. That bucket counts
        whole, an over-count of under one width — the conservative direction."""
        first = self._index(instant)
        totals = dict.fromkeys(UsageMetric, 0)
        for bucket in reversed(self._buckets):
            if bucket.index < first:
                break
            for metric, count in bucket.counts.items():
                totals[metric] += count
        return totals

    def __len__(self) -> int:
        return len(self._buckets)

    def _index(self, instant: float) -> int:
        return int(instant // self._width)

    def _prune(self, now: float) -> None:
        oldest_kept = self._index(now - self.retention)
        while self._buckets and self._buckets[0].index < oldest_kept:
            self._buckets.popleft()


class UsageLedger:
    """One agent's usage in this process. Reads return every :class:`UsageMetric`,
    zero when unused, so a reader never meets a missing key."""

    def __init__(self, fine: BucketSeries, coarse: BucketSeries, clock: Clock) -> None:
        self._fine = fine
        self._coarse = coarse
        self._clock = clock
        self._lock = threading.Lock()

    def record(self, amounts: Mapping[UsageMetric, int]) -> None:
        """Add ``amounts`` atomically, so an LLM call is never seen without its
        tokens. Non-positive amounts are dropped: the ledger is monotone."""
        positive = {metric: amount for metric, amount in amounts.items() if amount > 0}
        if not positive:
            return
        with self._lock:
            now = self._clock()
            self._fine.add(now, positive)
            self._coarse.add(now, positive)

    def since(self, instant: float) -> dict[UsageMetric, int]:
        """Usage from the monotonic ``instant`` to now."""
        with self._lock:
            return self._covering(self._clock() - instant).total_since(instant)

    def within(self, seconds: float) -> dict[UsageMetric, int]:
        """Usage over the trailing ``seconds``, a rolling window."""
        # One clock read, and the tier chosen by the span itself: a second read
        # would push a fine-retention window just past it, onto the coarse tier.
        with self._lock:
            return self._covering(seconds).total_since(self._clock() - seconds)

    def _covering(self, span: float) -> BucketSeries:
        return self._fine if span <= self._fine.retention else self._coarse


def new_usage_ledger(clock: Clock = time.monotonic) -> UsageLedger:
    return UsageLedger(
        fine=BucketSeries(FINE_BUCKET_SECONDS, FINE_RETENTION_SECONDS),
        coarse=BucketSeries(COARSE_BUCKET_SECONDS, MAX_WINDOW_SECONDS),
        clock=clock,
    )


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
