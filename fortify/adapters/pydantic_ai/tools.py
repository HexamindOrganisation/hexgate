"""Policy-gated wrappers around pydantic_ai ``Tool`` instances.

Each tool is wrapped so its ``function_schema.call`` consults a shared
:class:`~fortify.security.enforcer.PolicyEnforcer` before delegating to
the original implementation. pydantic_ai's idiom for "feed this back to
the model as a tool result" is :class:`~pydantic_ai.exceptions.ModelRetry`,
so denials and approval-required outcomes are raised as ModelRetry with a
rendered :class:`~fortify.security.decision.Decision` message.
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
    """Format a non-allow :class:`Decision` as the message body of a ModelRetry."""
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
    """Return a copy of ``tool`` with a policy gate installed.

    The original :class:`Tool` and its ``function_schema`` are left
    untouched — only the returned copy carries the gate. The gate
    replaces ``function_schema.call`` with a closure that asks the
    enforcer for a :class:`Decision` and either delegates (allow) or
    raises :class:`ModelRetry` with the rendered failure message
    (deny / approval-required).
    """
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
    """Return copies of ``tools``, each carrying a policy gate."""
    return [wrap_tool(t, enforcer) for t in tools]
