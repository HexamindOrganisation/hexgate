"""Tests for the process-local per-agent usage ledger behind ``agent_usage.*``.

Every failure here is silent — a window reading low fails a quota open, a missing
key fails it closed — so these tests pin the rounding, retention and registry
semantics the ``agent_usage.*`` namespace and PR 6 build on.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor

import pytest

from hexgate.runtime.agent_usage import (
    AGENT_USAGE_LEDGERS,
    FINE_BUCKET_SECONDS,
    FINE_RETENTION_SECONDS,
    HOUR_BUCKET_SECONDS,
    KNOWN_AGENT_USAGE_PATHS,
    MAX_WINDOW_SECONDS,
    MINUTE_BUCKET_SECONDS,
    USAGE_WINDOWS,
    BucketSeries,
    UsageLedger,
    UsageLedgers,
    UsageMetric,
    ledger_namespace,
    new_usage_ledger,
)
from hexgate.runtime.run_facts import KNOWN_RUN_PATHS

_WIDTH = 10.0
_RETENTION = 100.0
_START = 1_000.0
_DAY = 86_400.0
_HALF_HOUR = 1_800.0
_TWO_HOURS = 7_200.0
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
    [
        (_START + _RETENTION + _WIDTH, 1),
        (_START + _RETENTION - _WIDTH, 2),
        (_START + 3 * _RETENTION, 1),
    ],
    ids=["lapped-slot-starts-from-zero", "inside-retention-kept", "gap-past-the-ring"],
)
def test_a_bucket_past_retention_is_not_read(next_add: float, expected: int) -> None:
    series = BucketSeries(_WIDTH, _RETENTION)
    series.add(_START, _ONE_TOOL_CALL)

    series.add(next_add, _ONE_TOOL_CALL)

    assert series.total_since(_START)[UsageMetric.TOOL_CALLS] == expected


def test_two_adds_in_one_bucket_sum() -> None:
    series = BucketSeries(_WIDTH, _RETENTION)
    series.add(_START, _ONE_TOOL_CALL)
    series.add(_START + _WIDTH / 2, _ONE_TOOL_CALL)

    assert series.total_since(_START + _WIDTH / 2)[UsageMetric.TOOL_CALLS] == 2


def test_a_clock_step_backwards_counts_in_the_newest_bucket() -> None:
    series = BucketSeries(_WIDTH, _RETENTION)
    series.add(_START + _WIDTH, _ONE_TOOL_CALL)
    series.add(_START + 2 * _WIDTH, _ONE_TOOL_CALL)

    series.add(_START - 5 * _WIDTH, _ONE_TOOL_CALL)

    assert series.total_since(_START + _WIDTH)[UsageMetric.TOOL_CALLS] == 3
    assert series.total_since(_START + 2 * _WIDTH)[UsageMetric.TOOL_CALLS] == 2


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


def test_an_old_cut_off_reads_minute_buckets() -> None:
    clock = _FakeClock(_START * MINUTE_BUCKET_SECONDS)
    ledger = new_usage_ledger(clock=clock)
    ledger.record(_ONE_TOOL_CALL)
    clock.now += 2 * FINE_BUCKET_SECONDS
    ledger.record(_ONE_TOOL_CALL)
    clock.now += FINE_RETENTION_SECONDS + FINE_BUCKET_SECONDS

    cut_off = _START * MINUTE_BUCKET_SECONDS + 2 * FINE_BUCKET_SECONDS
    assert ledger.since(cut_off)[UsageMetric.TOOL_CALLS] == 2


class _TickingClock:
    """Advances on every read, like a real monotonic clock."""

    def __init__(self, now: float, tick: float = 1e-6) -> None:
        self.now = now
        self._tick = tick

    def __call__(self) -> float:
        self.now += self._tick
        return self.now


def test_a_fine_retention_window_reads_fine_buckets_on_a_ticking_clock() -> None:
    clock = _TickingClock(_START * MINUTE_BUCKET_SECONDS)
    ledger = new_usage_ledger(clock=clock)
    ledger.record(_ONE_TOOL_CALL)
    clock.now += FINE_RETENTION_SECONDS + 2 * FINE_BUCKET_SECONDS

    # Outside the 300 s window, but inside the minute bucket holding its cut-off.
    assert ledger.within(FINE_RETENTION_SECONDS)[UsageMetric.TOOL_CALLS] == 0


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


@pytest.mark.parametrize(
    ("age", "expected"),
    [(7 * _DAY + _HALF_HOUR, 1), (7 * _DAY + _TWO_HOURS, 0)],
    ids=["inside-the-leading-hour", "past-it"],
)
def test_a_week_window_rounds_out_to_the_hour(age: float, expected: int) -> None:
    clock = _FakeClock(_START * HOUR_BUCKET_SECONDS)
    ledger = new_usage_ledger(clock=clock)
    ledger.record(_ONE_TOOL_CALL)
    clock.now += age

    assert ledger.within(7 * _DAY)[UsageMetric.TOOL_CALLS] == expected


def test_an_idle_ledger_reads_zero_once_the_window_passes() -> None:
    clock = _FakeClock()
    ledger = new_usage_ledger(clock=clock)
    ledger.record(_ONE_TOOL_CALL)
    clock.now += 2 * _DAY

    assert ledger.within(_DAY)[UsageMetric.TOOL_CALLS] == 0
    assert ledger.within(MAX_WINDOW_SECONDS)[UsageMetric.TOOL_CALLS] == 1


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


class _CountingSeries(BucketSeries):
    def __init__(self) -> None:
        super().__init__(FINE_BUCKET_SECONDS, FINE_RETENTION_SECONDS)
        self.adds = 0

    def add(self, now: float, amounts: Mapping[UsageMetric, int]) -> None:
        self.adds += 1
        super().add(now, amounts)


def test_an_all_zero_record_touches_no_series() -> None:
    series = _CountingSeries()
    ledger = UsageLedger(series=(series,), clock=_FakeClock())

    ledger.record({UsageMetric.INPUT_TOKENS: 0, UsageMetric.OUTPUT_TOKENS: 0})

    assert series.adds == 0


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


def test_the_default_clock_follows_a_patched_monotonic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    ledger = new_usage_ledger()
    monkeypatch.setattr(time, "monotonic", clock)
    ledger.record(_ONE_TOOL_CALL)
    clock.now += 2 * _DAY

    assert ledger.since(_START)[UsageMetric.TOOL_CALLS] == 1
    assert ledger.within(_DAY)[UsageMetric.TOOL_CALLS] == 0


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
    """Only an enforcer over a usage policy enables it, and tests inject their own
    registry. Failing here means a test leaked into the process one."""
    assert AGENT_USAGE_LEDGERS.enabled is False


def test_metrics_share_their_names_with_run_paths() -> None:
    assert set(UsageMetric) - {UsageMetric.INVOCATIONS} <= KNOWN_RUN_PATHS


# ---------------------------------------------------------------------------
# Path registry and namespace
# ---------------------------------------------------------------------------

_HOUR = 3_600.0
_METRICS = (
    "invocations",
    "tool_calls",
    "denials",
    "llm_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
)
_WINDOWS = ("5m", "1h", "24h", "7d", "30d")


class _WindowCountingLedger(UsageLedger):
    def __init__(self, ledger: UsageLedger) -> None:
        self._inner = ledger
        self.windows_read: list[float] = []

    def within(self, seconds: float) -> dict[UsageMetric, int]:
        self.windows_read.append(seconds)
        return self._inner.within(seconds)


def _recorded_ledger(amounts: Mapping[UsageMetric, int]) -> UsageLedger:
    ledger = new_usage_ledger(clock=_FakeClock())
    ledger.record(amounts)
    return ledger


def test_the_registry_is_every_metric_over_every_window() -> None:
    expected = {f"{metric}_{window}" for metric in _METRICS for window in _WINDOWS}

    assert len(expected) == 35
    assert KNOWN_AGENT_USAGE_PATHS == expected


def test_no_window_outlasts_the_ledger() -> None:
    assert max(USAGE_WINDOWS.values()) <= MAX_WINDOW_SECONDS


def test_every_window_is_read_from_a_tier_that_retains_it() -> None:
    ledger = new_usage_ledger(clock=_FakeClock())

    for seconds in USAGE_WINDOWS.values():
        assert ledger._covering(seconds).retention >= seconds


def test_the_namespace_holds_exactly_the_requested_paths() -> None:
    ledger = _recorded_ledger(
        {UsageMetric.INPUT_TOKENS: 3, UsageMetric.OUTPUT_TOKENS: 4}
    )

    namespace = ledger_namespace(ledger, ["total_tokens_1h", "invocations_5m"])

    assert namespace == {"total_tokens_1h": 7, "invocations_5m": 0}


def test_the_namespace_reads_each_window_once() -> None:
    ledger = _WindowCountingLedger(_recorded_ledger(_ONE_TOOL_CALL))

    ledger_namespace(ledger, ["tool_calls_1h", "denials_1h", "tool_calls_24h"])

    assert sorted(ledger.windows_read) == [_HOUR, _DAY]


def test_the_namespace_rejects_an_unregistered_path() -> None:
    ledger = _recorded_ledger(_ONE_TOOL_CALL)

    with pytest.raises(KeyError):
        ledger_namespace(ledger, ["tool_call_1h"])


_TWO_INVOCATIONS = {UsageMetric.INVOCATIONS: 2, UsageMetric.TOOL_CALLS: 2}
_IN_RUN_PATHS = ["invocations_5m", "invocations_1h", "tool_calls_1h"]


@pytest.mark.parametrize(
    ("run_age", "expected"),
    [
        (None, {"invocations_5m": 2, "invocations_1h": 2, "tool_calls_1h": 2}),
        (10.0, {"invocations_5m": 1, "invocations_1h": 1, "tool_calls_1h": 2}),
        # Older than 5 m: its invocation has aged out of that window, not of 1 h.
        (400.0, {"invocations_5m": 2, "invocations_1h": 1, "tool_calls_1h": 2}),
    ],
    ids=["outside-a-run", "young-run", "run-older-than-a-window"],
)
def test_the_current_run_is_left_out_of_windows_still_holding_it(
    run_age: float | None, expected: dict[str, int]
) -> None:
    ledger = _recorded_ledger(_TWO_INVOCATIONS)

    namespace = ledger_namespace(ledger, _IN_RUN_PATHS, current_run_age=run_age)

    assert namespace == expected


def test_leaving_out_an_unrecorded_run_never_reads_negative() -> None:
    """A run opened before the ledger was enabled never recorded its invocation."""
    ledger = new_usage_ledger(clock=_FakeClock())

    assert ledger_namespace(ledger, ["invocations_1h"], current_run_age=1.0) == {
        "invocations_1h": 0
    }
