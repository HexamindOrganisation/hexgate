"""Cross-poll event_id dedup: the precondition for usage_minute's views.

ReplacingMergeTree forgives a duplicate insert; a materialized view sums it
forever. Duplicates come from produce retries (collector → Redpanda), which put
the same span on the topic again, usually in a later poll than the first copy.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from uuid import UUID

_log = logging.getLogger(__name__)

# 3x the collector's produce-retry horizon: its kafka exporter sets no
# retry_on_failure (platform/collector/config.yaml), so exporterhelper's
# default max_elapsed_time of 5 min applies.
DEDUP_WINDOW_MS = 15 * 60 * 1000
# Memory backstop for a burst the window was not sized for.
DEDUP_MAX_ENTRIES = 500_000
# Record time is the producer's clock (topic CreateTime). One record from a
# host whose clock jumped ahead must not drag the window forward and expire
# every live entry, so record time is capped at our wall clock plus this.
DEDUP_MAX_CLOCK_SKEW_MS = 60 * 1000

DedupKey = tuple[str, UUID]  # (project_id, event_id): the tables' own identity


def _wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


class RecentEventIds:
    """event_ids stored by this process within the last window of record time.

    The window is in Kafka record timestamps (produce time), not wall time: a
    retry's two copies are produced minutes apart however far behind this
    consumer is, so catch-up after an outage dedups like live traffic, and
    memory tracks produced volume rather than consumption speed. A timestamp
    ahead of the wall clock by more than the skew allowance is capped there.

    Process-local: a restart or rebalance loses it, and the replayed poll is
    over-counted in usage_minute — accepted, it errs toward denial. Remembers
    only whole acked inserts, so it cannot see blocks of a failed insert that
    landed anyway (see insert_decisions_batch); those over-count too.
    """

    def __init__(
        self,
        *,
        window_ms: int = DEDUP_WINDOW_MS,
        max_entries: int = DEDUP_MAX_ENTRIES,
        max_clock_skew_ms: int = DEDUP_MAX_CLOCK_SKEW_MS,
        wall_clock_ms: Callable[[], int] = _wall_clock_ms,
    ) -> None:
        self._window_ms = window_ms
        self._max_entries = max_entries
        self._max_clock_skew_ms = max_clock_skew_ms
        self._wall_clock_ms = wall_clock_ms
        self._seen: OrderedDict[DedupKey, int] = OrderedDict()
        self._newest_ms = 0

    def __contains__(self, key: DedupKey) -> bool:
        return key in self._seen

    def remember(self, stored: Iterable[tuple[DedupKey, int]]) -> None:
        """Record ``(key, record_timestamp_ms)`` pairs ClickHouse has acked."""
        ceiling_ms = self._wall_clock_ms() + self._max_clock_skew_ms
        clamped = 0
        for key, timestamp_ms in stored:
            if timestamp_ms > ceiling_ms:
                clamped += 1
                timestamp_ms = ceiling_ms
            self._seen[key] = timestamp_ms
            self._newest_ms = max(self._newest_ms, timestamp_ms)
        if clamped:
            _log.warning(
                "%d record timestamps more than %d ms ahead of the wall clock; "
                "capped so they cannot expire the dedup window early",
                clamped,
                self._max_clock_skew_ms,
            )
        self._expire()

    def _expire(self) -> None:
        # Front-only: partitions interleave, so insertion order is only roughly
        # timestamp order. An entry may outlive the window, never fall short of it.
        horizon = self._newest_ms - self._window_ms
        evicted_live = 0
        while self._seen:
            oldest_ms = next(iter(self._seen.values()))
            if oldest_ms >= horizon:
                if len(self._seen) <= self._max_entries:
                    break
                evicted_live += 1
            self._seen.popitem(last=False)
        if evicted_live:
            _log.warning(
                "event_id dedup cache full: evicted %d ids still inside the window; "
                "a retried span among them would be over-counted in usage_minute",
                evicted_live,
            )
