"""pydantic_ai adapter: wrap ``Tool.function_schema.call`` so it consults
a :class:`PolicyEnforcer` first. Non-allow outcomes raise
:class:`ModelRetry` with a rendered :class:`Decision` message (pydantic_ai's
idiom for feeding a tool failure back to the model).
"""

from __future__ import annotations

import copy
import functools
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import Tool

from fortify.security.decision import Decision, DecisionOutcome
from fortify.security.enforcer import PolicyEnforcer


def _render_decision(decision: Decision) -> str:
    """Format a non-allow :class:`Decision` as a ModelRetry message."""
    if decision.outcome is DecisionOutcome.NEEDS_APPROVAL:
        return (
            f"[approval_required] Tool '{decision.tool_name}' requires human "
            "approval before execution. The tool was not executed."
        )
    return (
        f"[policy_denied] Tool '{decision.tool_name}' is denied by the agent "
        "policy. The tool was not executed."
    )


def wrap_tool(tool: Tool, enforcer: PolicyEnforcer) -> Tool:
    """Return a copy of ``tool`` with ``function_schema.call`` gated by ``enforcer``."""
    name = tool.name
    tool_copy = copy.copy(tool)
    tool_copy.function_schema = copy.copy(tool.function_schema)
    original_call = tool_copy.function_schema.call

    @functools.wraps(original_call)
    async def guarded_call(args_dict: dict[str, Any], context: RunContext[Any]) -> Any:
        decision = enforcer.decide(name, args_dict or {})
        if decision.allowed:
            return await original_call(args_dict, context)
        raise ModelRetry(_render_decision(decision))

    tool_copy.function_schema.call = guarded_call
    return tool_copy


def wrap_tools(tools: list[Tool], enforcer: PolicyEnforcer) -> list[Tool]:
    """Return a fresh list of policy-gated copies."""
    return [wrap_tool(t, enforcer) for t in tools]
