"""dedup.py — the cross-poll event_id cache usage_minute's views depend on."""

from __future__ import annotations

import logging
import uuid

import pytest

from hexgate_api.jobs.enricher.dedup import DedupKey, RecentEventIds

WINDOW_MS = 1_000


def _key(project_id: str = "proj_1") -> DedupKey:
    return (project_id, uuid.uuid4())


def test_a_key_is_seen_only_once_remembered() -> None:
    recent = RecentEventIds(window_ms=WINDOW_MS)
    key = _key()

    assert key not in recent
    recent.remember([(key, 0)])
    assert key in recent


def test_the_same_event_id_in_two_projects_is_two_keys() -> None:
    recent = RecentEventIds(window_ms=WINDOW_MS)
    event_id = uuid.uuid4()

    recent.remember([(("proj_1", event_id), 0)])

    assert ("proj_2", event_id) not in recent


def test_a_key_older_than_the_window_expires_on_the_next_remember() -> None:
    recent = RecentEventIds(window_ms=WINDOW_MS)
    old, new = _key(), _key()

    recent.remember([(old, 0)])
    recent.remember([(new, WINDOW_MS + 1)])

    assert old not in recent
    assert new in recent


def test_a_key_exactly_at_the_window_edge_is_kept() -> None:
    recent = RecentEventIds(window_ms=WINDOW_MS)
    edge = _key()

    recent.remember([(edge, 0)])
    recent.remember([(_key(), WINDOW_MS)])

    assert edge in recent


def test_an_out_of_order_key_never_expires_before_its_own_window() -> None:
    """Partitions interleave, so a newer record can be remembered before an
    older one. Expiry must keep the older one until *its* window passes."""
    recent = RecentEventIds(window_ms=WINDOW_MS)
    newer, older = _key(), _key()

    recent.remember([(newer, 500), (older, 100)])
    recent.remember([(_key(), WINDOW_MS + 200)])

    assert older in recent  # held behind `newer`: late expiry is harmless
    assert newer in recent


def test_over_the_cap_the_oldest_is_evicted_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recent = RecentEventIds(window_ms=WINDOW_MS, max_entries=2)
    first, second, third = _key(), _key(), _key()

    with caplog.at_level(logging.WARNING):
        recent.remember([(first, 0), (second, 0), (third, 0)])

    assert len(recent) == 2
    assert first not in recent
    assert second in recent and third in recent
    assert "evicted 1 ids still inside the window" in caplog.text


def test_expiring_old_keys_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    recent = RecentEventIds(window_ms=WINDOW_MS, max_entries=1)

    with caplog.at_level(logging.WARNING):
        recent.remember([(_key(), 0)])
        recent.remember([(_key(), WINDOW_MS + 1)])

    assert len(recent) == 1
    assert caplog.text == ""
