"""Tests for async retry helpers."""

from __future__ import annotations

import pytest

from asianf.utils.retry import async_retry


@pytest.mark.asyncio
async def test_async_retry_retries_until_success() -> None:
    """Retry a failing coroutine until it succeeds."""
    attempts = 0

    @async_retry(retries=2, delay_ms=0, exceptions=(ValueError,))
    async def flaky() -> str:
        """Fail once before succeeding."""
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise ValueError("try again")
        return "ok"

    assert await flaky() == "ok"
    assert attempts == 2


@pytest.mark.asyncio
async def test_async_retry_reraises_after_exhaustion() -> None:
    """Raise the last matching exception after all retries."""

    @async_retry(retries=1, delay_ms=0, exceptions=(ValueError,))
    async def always_fail() -> None:
        """Always raise a matching exception."""
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await always_fail()


@pytest.mark.asyncio
async def test_async_retry_does_not_swallow_other_exceptions() -> None:
    """Propagate non-matching exceptions immediately."""
    attempts = 0

    @async_retry(retries=3, delay_ms=0, exceptions=(ValueError,))
    async def wrong_error() -> None:
        """Raise an exception outside the retry allowlist."""
        nonlocal attempts
        attempts += 1
        raise TypeError("wrong kind")

    with pytest.raises(TypeError, match="wrong kind"):
        await wrong_error()

    assert attempts == 1
