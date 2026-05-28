"""Google ADK adapter: wrap ``BaseTool`` so ``run_async`` consults a
:class:`PolicyEnforcer` first. Non-allow outcomes render as markered
strings the model sees as tool output.
"""

from __future__ import annotations

import copy
import functools
from collections.abc import Callable
from typing import Any, Union

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext

from fortify.security.enforcer import PolicyEnforcer


ToolEntry = Union[BaseTool, Callable[..., Any]]


def _normalize(tool: ToolEntry) -> BaseTool:
    """Coerce a tool entry into a ``BaseTool`` (plain callables → FunctionTool)."""
    if isinstance(tool, BaseTool):
        return tool
    if callable(tool):
        return FunctionTool(func=tool)
    raise TypeError(
        f"Cannot install policy on tool {tool!r}: expected google.adk BaseTool "
        f"or callable, got {type(tool).__name__}."
    )


def wrap_tool(tool: ToolEntry, enforcer: PolicyEnforcer) -> BaseTool:
    """Return a copy of ``tool`` with ``run_async`` gated by ``enforcer``."""
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
        return decision.as_error_message()

    wrapped = copy.copy(base)
    wrapped.run_async = guarded_run_async
    return wrapped


def wrap_tools(tools: list[ToolEntry], enforcer: PolicyEnforcer) -> list[BaseTool]:
    """Return a fresh list of policy-gated copies."""
    return [wrap_tool(t, enforcer) for t in tools]
