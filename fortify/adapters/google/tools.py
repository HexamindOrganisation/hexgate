"""Policy-gated wrappers around Google ADK ``BaseTool`` instances.

Each tool is wrapped so its ``run_async`` consults a shared
:class:`~fortify.security.enforcer.PolicyEnforcer` before delegating to
the original implementation. The ADK runtime expects a string tool
result, so denials and approval-required outcomes are rendered as short
strings the model can interpret — denied or approval-required calls
never reach the underlying tool.
"""

from __future__ import annotations

import copy
import functools
from collections.abc import Callable
from typing import Any, Union

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext

from fortify.security.decision import Decision, DecisionOutcome
from fortify.security.enforcer import PolicyEnforcer


ToolEntry = Union[BaseTool, Callable[..., Any]]


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


def _normalize(tool: ToolEntry) -> BaseTool:
    """Coerce an agent tool entry into a ``BaseTool``.

    Google ADK accepts plain callables in ``LlmAgent.tools`` and wraps
    them into ``FunctionTool`` internally. We do the same up front so
    the policy gate has a stable ``BaseTool`` surface to attach to.
    """
    if isinstance(tool, BaseTool):
        return tool
    if callable(tool):
        return FunctionTool(func=tool)
    raise TypeError(
        f"Cannot install policy on tool {tool!r}: expected google.adk BaseTool "
        f"or callable, got {type(tool).__name__}."
    )


def wrap_tool(tool: ToolEntry, enforcer: PolicyEnforcer) -> BaseTool:
    """Return a policy-gated copy of ``tool``.

    The original tool is left untouched. The copy shares all fields with
    the original except ``run_async``, which is replaced by a guard that
    consults the enforcer and either delegates (allow) or returns a
    rendered string (deny / approval-required).
    """
    base = _normalize(tool)
    name = base.name
    original_run_async = base.run_async

    @functools.wraps(original_run_async, updated=())
    async def guarded_run_async(
        *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        decision = enforcer.decide(name, args or {})
        if decision.allowed:
            return await original_run_async(args=args, tool_context=tool_context)
        return _render_decision(decision)

    wrapped = copy.copy(base)
    wrapped.run_async = guarded_run_async
    return wrapped


def wrap_tools(tools: list[ToolEntry], enforcer: PolicyEnforcer) -> list[BaseTool]:
    """Return a new list of policy-gated copies of ``tools``."""
    return [wrap_tool(t, enforcer) for t in tools]
