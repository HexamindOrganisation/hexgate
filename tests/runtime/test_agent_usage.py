"""Tests for the process-local per-agent usage ledger behind ``agent_usage.*``.

Every failure here is silent — a window reading low fails a quota open, a missing
key fails it closed — so these tests pin the rounding, retention and registry
semantics PR 2b and PR 6 build on.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hexgate.runtime.agent_usage import (
    AGENT_USAGE_LEDGERS,
    COARSE_BUCKET_SECONDS,
    FINE_BUCKET_SECONDS,
    FINE_RETENTION_SECONDS,
    MAX_WINDOW_SECONDS,
    BucketSeries,
    UsageLedger,
    UsageLedgers,
    UsageMetric,
    new_usage_ledger,
)
from hexgate.runtime.run_facts import KNOWN_RUN_PATHS

_WIDTH = 10.0
_RETENTION = 100.0
_START = 1_000.0
_DAY = 86_400.0
_WRITERS = 8
_WRITES_EACH = 500
_ONE_TOOL_CALL = {UsageMetric.TOOL_CALLS: 1}


class _FakeClock:
    def __init__(self, now: float = _START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# BucketSeries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cut_off", "expected"),
    [(_START + _WIDTH / 2, 1), (_START + _WIDTH, 0)],
    ids=["holding-bucket-counts-whole", "earlier-bucket-excluded"],
)
def test_the_bucket_holding_the_cut_off_counts_whole(
    cut_off: float, expected: int
) -> None:
    series = BucketSeries(_WIDTH, _RETENTION)
    series.add(_START, _ONE_TOOL_CALL)

    assert series.total_since(cut_off)[UsageMetric.TOOL_CALLS] == expected


@pytest.mark.parametrize(
    ("next_add", "expected"),
    [(_START + _RETENTION + _WIDTH, 1), (_START + _RETENTION - _WIDTH, 2)],
    ids=["past-retention-pruned", "inside-retention-kept"],
)
def test_buckets_past_retention_are_pruned_on_the_next_add(
    next_add: float, expected: int
) -> None:
    series = BucketSeries(_WIDTH, _RETENTION)
    series.add(_START, _ONE_TOOL_CALL)

    series.add(next_add, _ONE_TOOL_CALL)

    assert len(series) == expected
    assert series.total_since(_START)[UsageMetric.TOOL_CALLS] == expected


def test_two_adds_in_one_bucket_make_one_bucket() -> None:
    series = BucketSeries(_WIDTH, _RETENTION)
    series.add(_START, _ONE_TOOL_CALL)
    series.add(_START + _WIDTH / 2, _ONE_TOOL_CALL)

    assert len(series) == 1
    assert series.total_since(_START)[UsageMetric.TOOL_CALLS] == 2


# ---------------------------------------------------------------------------
# UsageLedger
# ---------------------------------------------------------------------------


def test_a_recent_cut_off_reads_fine_buckets() -> None:
    clock = _FakeClock()
    ledger = new_usage_ledger(clock=clock)
    ledger.record(_ONE_TOOL_CALL)
    clock.now += 2 * FINE_BUCKET_SECONDS
    ledger.record(_ONE_TOOL_CALL)
    clock.now += FINE_BUCKET_SECONDS

    cut_off = _START + 2 * FINE_BUCKET_SECONDS
    assert ledger.since(cut_off)[UsageMetric.TOOL_CALLS] == 1


def test_an_old_cut_off_reads_coarse_buckets() -> None:
    clock = _FakeClock(_START * COARSE_BUCKET_SECONDS)
    ledger = new_usage_ledger(clock=clock)
    ledger.record(_ONE_TOOL_CALL)
    clock.now += 2 * FINE_BUCKET_SECONDS
    ledger.record(_ONE_TOOL_CALL)
    clock.now += FINE_RETENTION_SECONDS + FINE_BUCKET_SECONDS

    cut_off = _START * COARSE_BUCKET_SECONDS + 2 * FINE_BUCKET_SECONDS
    assert ledger.since(cut_off)[UsageMetric.TOOL_CALLS] == 2


@pytest.mark.parametrize(("age_days", "expected"), [(29, 1), (31, 0)])
def test_the_longest_window_reaches_back_thirty_days(
    age_days: int, expected: int
) -> None:
    clock = _FakeClock()
    ledger = new_usage_ledger(clock=clock)
    ledger.record(_ONE_TOOL_CALL)
    clock.now += age_days * _DAY
    ledger.record({UsageMetric.DENIALS: 1})

    assert ledger.within(MAX_WINDOW_SECONDS)[UsageMetric.TOOL_CALLS] == expected


def test_every_read_returns_every_metric_zero_filled() -> None:
    ledger = new_usage_ledger(clock=_FakeClock())
    ledger.record(_ONE_TOOL_CALL)

    for read in (ledger.within(FINE_BUCKET_SECONDS), ledger.within(_DAY)):
        assert set(read) == set(UsageMetric)
        assert read[UsageMetric.TOOL_CALLS] == 1
        assert all(read[m] == 0 for m in UsageMetric if m != UsageMetric.TOOL_CALLS)


def test_record_drops_non_positive_amounts() -> None:
    ledger = new_usage_ledger(clock=_FakeClock())
    ledger.record({UsageMetric.LLM_CALLS: 1, UsageMetric.INPUT_TOKENS: -5})

    read = ledger.within(_DAY)
    assert read[UsageMetric.LLM_CALLS] == 1
    assert read[UsageMetric.INPUT_TOKENS] == 0


def test_an_all_zero_record_creates_no_bucket() -> None:
    fine = BucketSeries(FINE_BUCKET_SECONDS, FINE_RETENTION_SECONDS)
    coarse = BucketSeries(COARSE_BUCKET_SECONDS, MAX_WINDOW_SECONDS)
    ledger = UsageLedger(fine=fine, coarse=coarse, clock=_FakeClock())

    ledger.record({UsageMetric.INPUT_TOKENS: 0, UsageMetric.OUTPUT_TOKENS: 0})

    assert (len(fine), len(coarse)) == (0, 0)


def test_parallel_writers_lose_no_increments() -> None:
    ledger = new_usage_ledger()

    def write() -> None:
        for _ in range(_WRITES_EACH):
            ledger.record(_ONE_TOOL_CALL)

    threads = [threading.Thread(target=write) for _ in range(_WRITERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    total = ledger.within(MAX_WINDOW_SECONDS)[UsageMetric.TOOL_CALLS]
    assert total == _WRITERS * _WRITES_EACH


# ---------------------------------------------------------------------------
# UsageLedgers
# ---------------------------------------------------------------------------


def test_a_disabled_registry_hands_out_no_ledger() -> None:
    ledgers = UsageLedgers(new_usage_ledger)

    assert not ledgers.enabled
    assert ledgers.ledger_for("a") is None


def test_an_enabled_registry_keeps_one_ledger_per_agent() -> None:
    ledgers = UsageLedgers(new_usage_ledger)
    ledgers.enable()
    ledgers.enable()

    first = ledgers.ledger_for("a")
    assert first is not None
    assert ledgers.ledger_for("a") is first
    assert ledgers.ledger_for("b") is not first
    assert ledgers.enabled


def test_a_concurrent_first_lookup_yields_one_ledger() -> None:
    ledgers = UsageLedgers(new_usage_ledger)
    ledgers.enable()

    with ThreadPoolExecutor(max_workers=_WRITERS) as pool:
        found = list(pool.map(lambda _: ledgers.ledger_for("a"), range(_WRITERS * 4)))

    assert len({id(ledger) for ledger in found}) == 1


def test_the_process_registry_is_disabled_at_import() -> None:
    """Pins the roadmap's "inert on main" invariant until PR 2b enables it."""
    assert AGENT_USAGE_LEDGERS.enabled is False


def test_metrics_share_their_names_with_run_paths() -> None:
    assert set(UsageMetric) - {UsageMetric.INVOCATIONS} <= KNOWN_RUN_PATHS
