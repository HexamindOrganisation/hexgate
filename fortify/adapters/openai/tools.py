from __future__ import annotations

import copy
import functools
import json
from typing import Any

from agents import FunctionTool
from agents.tool import ToolContext

from fortify.security import (
    AgentPolicy,
    ApprovalRequiredError,
    PolicyDeniedError,
    authorize_tool_call,
)


def _denial_message(tool_name: str) -> str:
    return (
        f"Tool '{tool_name}' is denied by the agent policy. The tool was not executed."
    )


def _parse_args(raw: str) -> dict[str, Any] | None:
    """Best-effort parse of a tool-call JSON payload into a dict for policy checks."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def wrap_tool(tool: FunctionTool, policy: AgentPolicy) -> FunctionTool:
    """Return a policy-gated copy of `tool`.

    The original tool is left untouched. The copy shares all fields with
    the original except `on_invoke_tool`, which is replaced by a guard
    that consults the policy before delegating.

    On denial, the wrapper returns the denial text rather than raising,
    so the OpenAI Agents runtime forwards it to the model as the tool
    output instead of aborting the run — matching the LangChain wrapper's
    `handle_tool_error=True` behavior.
    """
    if not isinstance(tool, FunctionTool):
        raise TypeError(
            f"Cannot install policy on tool {getattr(tool, 'name', tool)!r}: "
            f"expected agents.FunctionTool, got {type(tool).__name__}. "
            "Wrapping only supports function tools."
        )

    name = tool.name
    original_invoke = tool.on_invoke_tool

    @functools.wraps(original_invoke, updated=())
    async def guarded_invoke(ctx: ToolContext[Any], input: str) -> Any:
        try:
            authorize_tool_call(policy, name, _parse_args(input))
        except (PolicyDeniedError, ApprovalRequiredError):
            return _denial_message(name)
        return await original_invoke(ctx, input)

    wrapped = copy.copy(tool)
    wrapped.on_invoke_tool = guarded_invoke
    return wrapped


def wrap_tools(tools: list[FunctionTool], policy: AgentPolicy) -> list[FunctionTool]:
    """Return a new list of policy-gated copies of `tools`."""
    return [wrap_tool(t, policy) for t in tools]
