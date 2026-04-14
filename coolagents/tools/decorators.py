"""Decorator helpers for agent tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from langchain_core.tools import tool

from coolagents.tracing.langfuse import observe
from coolagents.utils.retry import async_retry

TOOL_METADATA_ATTR = "__tool_metadata__"
CallFormatter = Callable[[dict[str, Any]], str]


def _default_call_label(tool_name: str, arguments: dict[str, Any]) -> str:
    """Return the default human-readable label for a tool call."""
    _ = arguments
    return tool_name


def format_tool_call_label(tool: Any, arguments: dict[str, Any] | None = None) -> str:
    """Return a human-readable label for a tool call."""
    tool_name = getattr(tool, "name", None) or getattr(tool, "__name__", None) or "tool"
    metadata = getattr(tool, TOOL_METADATA_ATTR, {})
    formatter = metadata.get("call_formatter")
    payload = arguments or {}

    if callable(formatter):
        try:
            label = formatter(payload)
        except Exception:
            label = ""
        if isinstance(label, str) and label.strip():
            return label

    return _default_call_label(str(tool_name), payload)


def agent_tool(
    *,
    name: str,
    retries: int = 3,
    delay_ms: int = 1000,
    exceptions: tuple[type[BaseException], ...] = (httpx.HTTPError,),
    call_formatter: CallFormatter | None = None,
) -> Callable[[Callable[..., Any]], Any]:
    """Compose tracing, retry, and tool registration for agent tools."""

    def decorator(func: Callable[..., Any]) -> Any:
        """Wrap a function with the standard tool stack."""
        wrapped = async_retry(
            retries=retries,
            delay_ms=delay_ms,
            exceptions=exceptions,
        )(func)
        wrapped = observe(name=name)(wrapped)
        registered_tool = tool(wrapped)
        setattr(
            registered_tool,
            TOOL_METADATA_ATTR,
            {
                "call_formatter": call_formatter,
            },
        )
        return registered_tool

    return decorator
