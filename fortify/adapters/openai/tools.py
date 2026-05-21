"""OpenAI Agents adapter: wrap ``FunctionTool`` so ``on_invoke_tool``
consults a :class:`PolicyEnforcer` first. Non-allow outcomes render as
markered strings the model sees as tool output.
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
    """Best-effort JSON-to-dict parse of a tool-call payload."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _render_decision(decision: Decision) -> str:
    """Format a non-allow :class:`Decision` as a string tool result."""
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
    """Return a copy of ``tool`` with ``on_invoke_tool`` gated by ``enforcer``."""
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
    """Return a fresh list of policy-gated copies."""
    return [wrap_tool(t, enforcer) for t in tools]
