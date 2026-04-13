"""Async retry utilities."""

import asyncio

from functools import wraps


def async_retry(retries: int = 3, delay_ms: int = 200, exceptions=(Exception,)):
    """Retry an async function for a subset of failures."""
    delay = delay_ms / 1000.0

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as err:
                    last_err = err
                    if attempt < retries:
                        await asyncio.sleep(delay)
                    else:
                        raise last_err

        return wrapper

    return decorator
