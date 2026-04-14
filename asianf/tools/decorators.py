"""Decorator helpers for agent tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from langchain_core.tools import tool

from asianf.tracing.langfuse import observe
from asianf.utils.retry import async_retry


def agent_tool(
    *,
    name: str,
    retries: int = 3,
    delay_ms: int = 1000,
    exceptions: tuple[type[BaseException], ...] = (httpx.HTTPError,),
) -> Callable[[Callable[..., Any]], Any]:
    """Compose tracing, retry, and tool registration for agent tools."""

    def decorator(func: Callable[..., Any]) -> Any:
        """Wrap a function with the standard asianf tool stack."""
        wrapped = async_retry(
            retries=retries,
            delay_ms=delay_ms,
            exceptions=exceptions,
        )(func)
        wrapped = observe(name=name)(wrapped)
        return tool(wrapped)

    return decorator
