"""Langfuse integration helpers."""

from __future__ import annotations

from inspect import signature
from typing import Any, Protocol

from langfuse import get_client, observe as langfuse_observe
from langfuse.langchain import CallbackHandler
from langchain_core.runnables import RunnableConfig


class LangfuseHandler(Protocol):
    """Protocol for the Langfuse callback handler used by hexgate."""

    last_trace_id: str | None
    langfuse_metadata: dict[str, Any]


def observe(*args, **kwargs):
    """Apply the Langfuse observe decorator."""
    return langfuse_observe(*args, **kwargs)


def get_langfuse_handler(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
) -> CallbackHandler:
    """Create a Langfuse LangChain callback handler."""
    init_params = signature(CallbackHandler.__init__).parameters
    default_tags = tags or ["hexgate"]

    if "session_id" in init_params:
        return CallbackHandler(
            session_id=session_id,
            user_id=user_id,
            tags=default_tags,
        )

    handler = CallbackHandler()
    handler.langfuse_metadata = {
        "langfuse_session_id": session_id,
        "langfuse_user_id": user_id,
        "langfuse_tags": default_tags,
    }
    return handler


def get_langfuse_runnable_config(handler: CallbackHandler) -> RunnableConfig:
    """Build LangChain runnable config for the current Langfuse SDK."""
    config: RunnableConfig = {"callbacks": [handler]}
    metadata = getattr(handler, "langfuse_metadata", None)
    if metadata:
        config["metadata"] = {k: v for k, v in metadata.items() if v is not None}

    return config


def maybe_get_trace_url(handler: CallbackHandler | None = None) -> str | None:
    """Return the current trace URL if Langfuse is active."""
    client = get_client()
    trace_id = getattr(handler, "last_trace_id", None) if handler is not None else None
    try:
        return client.get_trace_url(trace_id=trace_id)
    except Exception:
        return None
