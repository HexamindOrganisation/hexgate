from __future__ import annotations

import functools
from typing import Any

from langchain_core.tools import BaseTool, ToolException

from fortify.security import (
    AgentPolicy,
    ApprovalRequiredError,
    PolicyDeniedError,
    authorize_tool_call,
)


class ToolDeniedError(ToolException):
    """Raised when a tool call is blocked by the `AgentPolicy`.

    Inherits from `ToolException` so that `BaseTool.run` catches it
    (when `handle_tool_error=True` on the tool) and turns the denial
    message into tool output content, rather than letting it bubble up
    and abort the graph.
    """

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        message = (
            f"Tool '{tool_name}' is denied by the agent policy. "
            "The tool was not executed."
        )
        super().__init__(message)


def wrap_tool(tool: BaseTool, policy: AgentPolicy) -> BaseTool:
    """Install a policy gate on `tool` in place.

    Returns the same object so call sites can keep chaining, but the
    gate is applied by mutation.
    """
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
            try:
                authorize_tool_call(policy, name, kwargs)
            except (PolicyDeniedError, ApprovalRequiredError):
                raise ToolDeniedError(name)
            return original_func(*args, **kwargs)

        tool.func = guarded_func

    if original_coroutine is not None:

        @functools.wraps(original_coroutine)
        async def guarded_coroutine(*args: Any, **kwargs: Any) -> Any:
            try:
                authorize_tool_call(policy, name, kwargs)
            except (PolicyDeniedError, ApprovalRequiredError):
                raise ToolDeniedError(name)
            return await original_coroutine(*args, **kwargs)

        tool.coroutine = guarded_coroutine

    tool.handle_tool_error = True
    return tool


def wrap_tools(tools: list[BaseTool], policy: AgentPolicy) -> list[BaseTool]:
    """Install policy gates on `tools` in place, returning the same list."""
    for t in tools:
        wrap_tool(t, policy)
    return tools
