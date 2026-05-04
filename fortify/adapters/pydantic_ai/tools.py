from __future__ import annotations

import copy
import functools
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import Tool

from fortify.security import (
    AgentPolicy,
    ApprovalRequiredError,
    PolicyDeniedError,
    authorize_tool_call,
)


_active_policy: ContextVar[AgentPolicy | None] = ContextVar(
    "fortify_active_policy_pydantic_ai", default=None
)


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


class ToolDeniedError(ModelRetry):
    """Raised when a tool call is blocked by the `AgentPolicy`.

    Inherits from `ModelRetry` so pydantic_ai surfaces the denial back
    to the model as a tool-result message rather than aborting the run,
    matching the behavior of the Langchain wrapper's `ToolException`.
    """

    def __init__(self, tool_name: str, reason: str | None = None) -> None:
        self.tool_name = tool_name
        suffix = f" ({reason})" if reason else ""
        message = (
            f"Tool '{tool_name}' is denied by the agent policy{suffix}. "
            "The tool was not executed."
        )
        super().__init__(message)


def wrap_tool(tool: Tool) -> Tool:
    """Return a copy of `tool` with a policy gate installed.

    The original `Tool` and its `function_schema` are left untouched —
    only the returned copy carries the gate. The gate is applied by
    overriding `function_schema.call`, the single async entrypoint
    pydantic_ai uses to invoke the tool. The gate consults the
    `_active_policy` contextvar at call time, so the same wrapped tool
    can serve many users concurrently.
    """
    name = tool.name
    tool_copy = copy.copy(tool)
    tool_copy.function_schema = copy.copy(tool.function_schema)
    original_call = tool_copy.function_schema.call

    @functools.wraps(original_call)
    async def guarded_call(args_dict: dict[str, Any], context: RunContext[Any]) -> Any:
        policy = _active_policy.get()
        if policy is None:
            raise ToolDeniedError(name, "no active Fortify policy")
        try:
            authorize_tool_call(policy, name, args_dict)
        except (PolicyDeniedError, ApprovalRequiredError):
            raise ToolDeniedError(name)
        return await original_call(args_dict, context)

    tool_copy.function_schema.call = guarded_call
    return tool_copy


def wrap_tools(tools: list[Tool]) -> list[Tool]:
    """Return copies of `tools`, each carrying a policy gate."""
    return [wrap_tool(t) for t in tools]
