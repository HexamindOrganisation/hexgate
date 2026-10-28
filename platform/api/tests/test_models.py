"""``as_utc``: the one place a naive stored timestamp is read as UTC."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hexgate_api.models import as_utc, utcnow


def test_a_naive_datetime_is_stamped_utc_without_shifting() -> None:
    naive = datetime(2026, 5, 4, 7, 8, 9)

    stamped = as_utc(naive)

    assert stamped == datetime(2026, 5, 4, 7, 8, 9, tzinfo=timezone.utc)
    assert stamped.tzinfo is timezone.utc
    assert stamped < utcnow()  # naive vs aware would raise TypeError


def test_an_aware_datetime_is_returned_unchanged() -> None:
    utc = datetime(2026, 5, 4, 7, 8, 9, tzinfo=timezone.utc)
    paris = datetime(2026, 5, 4, 9, 8, 9, tzinfo=timezone(timedelta(hours=2)))

    assert as_utc(utc) is utc
    assert as_utc(paris) is paris
