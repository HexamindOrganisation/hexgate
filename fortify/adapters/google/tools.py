from __future__ import annotations

import copy
import functools
from typing import Any, Callable, Union

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext

from fortify.security import (
    AgentPolicy,
    ApprovalRequiredError,
    PolicyDeniedError,
    authorize_tool_call,
)


ToolEntry = Union[BaseTool, Callable[..., Any]]


def _denial_message(tool_name: str) -> str:
    return (
        f"Tool '{tool_name}' is denied by the agent policy. The tool was not executed."
    )


def _normalize(tool: ToolEntry) -> BaseTool:
    """Coerce an agent tool entry into a `BaseTool`.

    Google ADK accepts plain callables in `LlmAgent.tools` and wraps them
    into `FunctionTool` internally. We do the same up front so the policy
    gate has a stable `BaseTool` surface to attach to.
    """
    if isinstance(tool, BaseTool):
        return tool
    if callable(tool):
        return FunctionTool(func=tool)
    raise TypeError(
        f"Cannot install policy on tool {tool!r}: expected google.adk BaseTool "
        f"or callable, got {type(tool).__name__}."
    )


def wrap_tool(tool: ToolEntry, policy: AgentPolicy) -> BaseTool:
    """Return a policy-gated copy of `tool`.

    The original tool is left untouched. The copy shares all fields with
    the original except `run_async`, which is replaced by a guard that
    consults the policy before delegating.

    On denial, the wrapper returns the denial text rather than raising,
    so the ADK runtime forwards it to the model as the tool output
    instead of aborting the run — matching the OpenAI wrapper's
    behavior.
    """
    base = _normalize(tool)
    name = base.name
    original_run_async = base.run_async

    @functools.wraps(original_run_async, updated=())
    async def guarded_run_async(
        *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        try:
            authorize_tool_call(policy, name, args)
        except (PolicyDeniedError, ApprovalRequiredError):
            return _denial_message(name)
        return await original_run_async(args=args, tool_context=tool_context)

    wrapped = copy.copy(base)
    wrapped.run_async = guarded_run_async
    return wrapped


def wrap_tools(tools: list[ToolEntry], policy: AgentPolicy) -> list[BaseTool]:
    """Return a new list of policy-gated copies of `tools`."""
    return [wrap_tool(t, policy) for t in tools]
