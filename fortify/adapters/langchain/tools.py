from __future__ import annotations

import functools
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from langchain_core.tools import BaseTool, ToolException

from fortify.security import (
    AgentPolicy,
    ApprovalRequiredError,
    PolicyDeniedError,
    authorize_tool_call,
)


_active_policy: ContextVar[AgentPolicy | None] = ContextVar(
    "fortify_active_policy", default=None
)

_FORTIFY_WRAPPED_ATTR = "_fortify_wrapped"


@contextmanager
def active_policy(policy: AgentPolicy) -> Iterator[None]:
    """Bind `policy` for the current async/thread context.

    Tool gates installed by `wrap_tool` consult this contextvar at call
    time, so callers must enter this context manager before invoking the
    agent. `ContextVar` is per-task, so concurrent invocations for
    different users do not see each other's policies.
    """
    token = _active_policy.set(policy)
    try:
        yield
    finally:
        _active_policy.reset(token)


class ToolDeniedError(ToolException):
    """Raised when a tool call is blocked by the `AgentPolicy`.

    Inherits from `ToolException` so that `BaseTool.run` catches it
    (when `handle_tool_error=True` on the tool) and turns the denial
    message into tool output content, rather than letting it bubble up
    and abort the graph.
    """

    def __init__(self, tool_name: str, reason: str | None = None) -> None:
        self.tool_name = tool_name
        suffix = f" ({reason})" if reason else ""
        message = (
            f"Tool '{tool_name}' is denied by the agent policy {suffix}. "
            "The tool was not executed."
        )
        super().__init__(message)


def wrap_tool(tool: BaseTool) -> BaseTool:
    """Install a contextvar-driven policy gate on `tool` in place.

    Returns the same object so call sites can keep chaining. Idempotent:
    a tool that has already been wrapped is returned untouched.
    """
    if getattr(tool, _FORTIFY_WRAPPED_ATTR, False):
        return tool

    name = tool.name
    original_func = getattr(tool, "func", None)
    original_coroutine = getattr(tool, "coroutine", None)

    if original_func is None and original_coroutine is None:
        raise TypeError(
            f"Cannot install policy on tool {name!r}: it is a "
            f"{type(tool).__name__} without `func`/`coroutine` attributes. "
            "In-place wrapping only supports StructuredTool-style tools."
        )

    if original_func is not None:

        @functools.wraps(original_func)
        def guarded_func(*args: Any, **kwargs: Any) -> Any:
            policy = _active_policy.get()
            if policy is None:
                raise ToolDeniedError(name, "no active Fortify policy")
            try:
                authorize_tool_call(policy, name, kwargs)
            except (PolicyDeniedError, ApprovalRequiredError):
                raise ToolDeniedError(name)
            return original_func(*args, **kwargs)

        tool.func = guarded_func

    if original_coroutine is not None:

        @functools.wraps(original_coroutine)
        async def guarded_coroutine(*args: Any, **kwargs: Any) -> Any:
            policy = _active_policy.get()
            if policy is None:
                raise ToolDeniedError(name, "no active Fortify policy")
            try:
                authorize_tool_call(policy, name, kwargs)
            except (PolicyDeniedError, ApprovalRequiredError):
                raise ToolDeniedError(name)
            return await original_coroutine(*args, **kwargs)

        tool.coroutine = guarded_coroutine

    tool.handle_tool_error = True
    setattr(tool, _FORTIFY_WRAPPED_ATTR, True)
    return tool


def wrap_tools(tools: list[BaseTool]) -> list[BaseTool]:
    """Install policy gates on `tools` in place, returning the same list."""
    for t in tools:
        wrap_tool(t)
    return tools
