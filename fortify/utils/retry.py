"""Async retry utilities."""

from __future__ import annotations

import asyncio
from functools import wraps
from typing import Any

import httpx


def _is_retryable_http_error(error: httpx.HTTPError) -> bool:
    """Return whether an HTTP-layer error should be retried."""
    if isinstance(error, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        return status_code == 429 or 500 <= status_code < 600
    return False


def is_retryable_error(error: BaseException) -> bool:
    """Return whether an exception should be retried."""
    if isinstance(error, httpx.HTTPError):
        return _is_retryable_http_error(error)
    return True


def async_retry(
    retries: int = 3,
    delay_ms: int = 200,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Any:
    """Retry an async function for a subset of failures."""
    delay = delay_ms / 1000.0

    def decorator(func: Any) -> Any:
        """Wrap an async function with retry behavior."""

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            """Execute the wrapped async function with retries."""
            last_err = None
            for attempt in range(retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as err:
                    last_err = err
                    if not is_retryable_error(err):
                        raise last_err
                    if attempt < retries:
                        await asyncio.sleep(delay)
                    else:
                        raise last_err

        return wrapper

    return decorator
