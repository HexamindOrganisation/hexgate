from __future__ import annotations

import copy
import functools
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import Tool

from fortify.security import (
    AgentPolicy,
    ApprovalRequiredError,
    PolicyDeniedError,
    authorize_tool_call,
)


class ToolDeniedError(ModelRetry):
    """Raised when a tool call is blocked by the `AgentPolicy`.

    Inherits from `ModelRetry` so pydantic_ai surfaces the denial back
    to the model as a tool-result message rather than aborting the run,
    matching the behavior of the Langchain wrapper's `ToolException`.
    """

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        message = (
            f"Tool '{tool_name}' is denied by the agent policy. "
            "The tool was not executed."
        )
        super().__init__(message)


def wrap_tool(tool: Tool, policy: AgentPolicy) -> Tool:
    """Return a copy of `tool` with a policy gate installed.

    The original `Tool` and its `function_schema` are left untouched —
    only the returned copy carries the gate. The gate is applied by
    overriding `function_schema.call`, the single async entrypoint
    pydantic_ai uses to invoke the tool.
    """
    name = tool.name
    tool_copy = copy.copy(tool)
    tool_copy.function_schema = copy.copy(tool.function_schema)
    original_call = tool_copy.function_schema.call

    @functools.wraps(original_call)
    async def guarded_call(args_dict: dict[str, Any], ctx: RunContext[Any]) -> Any:
        try:
            authorize_tool_call(policy, name, args_dict)
        except (PolicyDeniedError, ApprovalRequiredError):
            raise ToolDeniedError(name)
        return await original_call(args_dict, ctx)

    tool_copy.function_schema.call = guarded_call
    return tool_copy


def wrap_tools(tools: list[Tool], policy: AgentPolicy) -> list[Tool]:
    """Return copies of `tools`, each carrying a policy gate."""
    return [wrap_tool(t, policy) for t in tools]
