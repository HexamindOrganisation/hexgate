"""Policy-gated wrappers around OpenAI Agents ``FunctionTool`` instances.

Each tool is wrapped so its ``on_invoke_tool`` consults a shared
:class:`~fortify.security.enforcer.PolicyEnforcer` before delegating to the
original implementation. The OpenAI Agents runtime expects a string tool
result, so denials and approval-required outcomes are rendered as short
strings the model can interpret — denied or approval-required calls never
reach the underlying tool.
"""

from __future__ import annotations

import copy
import functools
import json
from typing import Any

from agents import FunctionTool
from agents.tool import ToolContext

from fortify.security.decision import Decision, DecisionOutcome
from fortify.security.enforcer import PolicyEnforcer


def _parse_args(raw: str) -> dict[str, Any] | None:
    """Best-effort parse of a tool-call JSON payload into a dict for policy checks."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _render_decision(decision: Decision) -> str:
    """Format a non-allow :class:`Decision` as the LLM-facing tool result."""
    if decision.outcome is DecisionOutcome.NEEDS_APPROVAL:
        return (
            f"[approval_required] Tool '{decision.tool_name}' requires human "
            "approval before execution. The tool was not executed."
        )
    return (
        f"[policy_denied] Tool '{decision.tool_name}' is denied by the agent "
        "policy. The tool was not executed."
    )


def wrap_tool(tool: FunctionTool, enforcer: PolicyEnforcer) -> FunctionTool:
    """Return a policy-gated copy of ``tool``.

    The original tool is left untouched. The copy shares all fields with
    the original except ``on_invoke_tool``, which is replaced by a guard
    that asks the enforcer for a :class:`Decision` and either delegates
    (allow) or returns a rendered denial string (deny / approval-required).
    """
    if not isinstance(tool, FunctionTool):
        raise TypeError(
            f"Cannot install policy on tool {getattr(tool, 'name', tool)!r}: "
            f"expected agents.FunctionTool, got {type(tool).__name__}. "
        )

    name = tool.name
    original_invoke = tool.on_invoke_tool

    @functools.wraps(original_invoke, updated=())
    async def guarded_invoke(ctx: ToolContext[Any], input: str) -> Any:
        decision = enforcer.decide(name, _parse_args(input) or {})
        if decision.allowed:
            return await original_invoke(ctx, input)
        return _render_decision(decision)

    wrapped = copy.copy(tool)
    wrapped.on_invoke_tool = guarded_invoke
    return wrapped


def wrap_tools(
    tools: list[FunctionTool], enforcer: PolicyEnforcer
) -> list[FunctionTool]:
    """Return a new list of policy-gated copies of ``tools``."""
    return [wrap_tool(t, enforcer) for t in tools]
