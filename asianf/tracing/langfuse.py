"""Langfuse integration helpers."""

from __future__ import annotations

from inspect import signature
from typing import Any, Callable


def observe(*args, **kwargs):
    """Use Langfuse observe when available, otherwise fall back to a no-op decorator."""
    try:
        from langfuse import observe as langfuse_observe
    except Exception:
        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            return args[0]

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            return func

        return decorator

    return langfuse_observe(*args, **kwargs)


def get_langfuse_handler(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
):
    """Create a Langfuse LangChain callback handler when the SDK is installed."""
    from langfuse.langchain import CallbackHandler

    init_params = signature(CallbackHandler.__init__).parameters
    default_tags = tags or ["asianf"]

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


def get_langfuse_runnable_config(handler: Any) -> dict[str, Any]:
    """Build LangChain runnable config for the current Langfuse SDK."""
    config: dict[str, Any] = {"callbacks": [handler]}
    metadata = getattr(handler, "langfuse_metadata", None)
    if metadata:
        config["metadata"] = {k: v for k, v in metadata.items() if v is not None}

    return config


def maybe_get_trace_url(handler: Any | None = None) -> str | None:
    """Return the current trace URL if Langfuse is active."""
    try:
        from langfuse import get_client
    except Exception:
        return None

    client = get_client()
    trace_id = getattr(handler, "last_trace_id", None) if handler is not None else None
    try:
        return client.get_trace_url(trace_id=trace_id)
    except Exception:
        return None
