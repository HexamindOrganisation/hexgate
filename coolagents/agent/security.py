"""Apply security concerns to created agent runtimes."""

from __future__ import annotations

from collections.abc import Sequence
from inspect import isawaitable
from typing import Any

from langchain_core.tools import BaseTool, tool

from coolagents.agent.factory import (
    AgentGraph,
    BeforeActionHook,
    ContextProvider,
    ToolSpec,
)
from coolagents.security import AgentPolicy
from coolagents.tools.decorators import TOOL_METADATA_ATTR


def _copy_tool_metadata(source: Any, target: Any) -> Any:
    """Copy coolagents-specific metadata from one tool object to another."""
    metadata = getattr(source, TOOL_METADATA_ATTR, None)
    if metadata is not None:
        setattr(target, TOOL_METADATA_ATTR, metadata)
    return target


def wrap_tools_with_policy(
    tools: Sequence[ToolSpec],
    policy: AgentPolicy | None,
) -> list[ToolSpec]:
    """Wrap tools so policy decisions are enforced before execution."""
    if policy is None:
        return list(tools)

    wrapped_tools: list[ToolSpec] = []
    for tool_spec in tools:
        if not isinstance(tool_spec, BaseTool):
            wrapped_tools.append(tool_spec)
            continue

        async def authorized_tool(
            _tool: BaseTool = tool_spec,
            **kwargs: Any,
        ) -> Any:
            """Authorize a tool call before delegating to the real tool."""
            from coolagents.security import authorize_tool_call

            authorize_tool_call(policy, _tool.name)
            return await _tool.ainvoke(kwargs)

        wrapped = tool(
            tool_spec.name,
            description=tool_spec.description,
            return_direct=tool_spec.return_direct,
            args_schema=tool_spec.args_schema,
            infer_schema=False,
        )(authorized_tool)
        wrapped_tools.append(_copy_tool_metadata(tool_spec, wrapped))
    return wrapped_tools


def enforce_policy(
    agent: AgentGraph,
    policy: str | AgentPolicy | None,
) -> AgentGraph:
    """Return an agent runtime with Gate 1 policy enforcement applied."""
    return agent.enforce_policy(policy)


def wrap_tools_with_before_action(
    tools: Sequence[ToolSpec],
    before_action: BeforeActionHook,
    *,
    context_provider: ContextProvider | None = None,
    agent_name: str | None = None,
) -> list[ToolSpec]:
    """Wrap tools so a hosted hook runs before the real tool executes."""
    wrapped_tools: list[ToolSpec] = []
    for tool_spec in tools:
        if not isinstance(tool_spec, BaseTool):
            wrapped_tools.append(tool_spec)
            continue

        async def guarded_tool(
            _tool: BaseTool = tool_spec,
            **kwargs: Any,
        ) -> Any:
            """Invoke the pre-tool hook before delegating to the real tool."""
            action = {
                "tool_name": _tool.name,
                "arguments": kwargs,
                "agent_name": agent_name,
            }
            context = context_provider() if context_provider is not None else None
            decision = before_action(action, context)
            if isawaitable(decision):
                await decision
            return await _tool.ainvoke(kwargs)

        wrapped = tool(
            tool_spec.name,
            description=tool_spec.description,
            return_direct=tool_spec.return_direct,
            args_schema=tool_spec.args_schema,
            infer_schema=False,
        )(guarded_tool)
        wrapped_tools.append(_copy_tool_metadata(tool_spec, wrapped))
    return wrapped_tools


def with_before_action(
    agent: AgentGraph,
    before_action: BeforeActionHook,
    *,
    context_provider: ContextProvider | None = None,
) -> AgentGraph:
    """Return an agent runtime with a Gate 2 pre-tool hook applied."""
    return agent.with_before_action(before_action, context_provider=context_provider)
