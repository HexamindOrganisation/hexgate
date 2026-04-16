"""Apply security concerns to created agent runtimes."""

from __future__ import annotations

from collections.abc import Sequence
from inspect import isawaitable
from typing import Any

from langchain_core.tools import BaseTool
from langchain_core.tools.structured import StructuredTool
from pydantic import ConfigDict

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


class GuardedTool(BaseTool):
    """A tool wrapper that enforces local policy and hosted hooks inline."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    wrapped_tool: BaseTool
    policy: AgentPolicy | None = None
    before_action: BeforeActionHook | None = None
    context_provider: ContextProvider | None = None
    agent_name: str | None = None

    async def _invoke_wrapped_async(self, *args: Any, **kwargs: Any) -> Any:
        """Call the original tool implementation without creating another tool run."""
        if isinstance(self.wrapped_tool, StructuredTool):
            if self.wrapped_tool.coroutine is not None:
                return await self.wrapped_tool.coroutine(*args, **kwargs)
            if self.wrapped_tool.func is not None:
                return self.wrapped_tool.func(*args, **kwargs)
        return await self.wrapped_tool._arun(*args, **kwargs)

    def _invoke_wrapped_sync(self, *args: Any, **kwargs: Any) -> Any:
        """Call the original sync implementation without re-entering tool instrumentation."""
        if isinstance(self.wrapped_tool, StructuredTool) and self.wrapped_tool.func is not None:
            return self.wrapped_tool.func(*args, **kwargs)
        return self.wrapped_tool._run(*args, **kwargs)

    def _authorize(self) -> None:
        """Apply local Gate 1 authorization when configured."""
        if self.policy is None:
            return
        from coolagents.security import authorize_tool_call

        authorize_tool_call(self.policy, self.name)

    async def _check_before_action(self, kwargs: dict[str, Any]) -> None:
        """Apply the hosted Gate 2 hook when configured."""
        if self.before_action is None:
            return
        action = {
            "tool_name": self.name,
            "arguments": kwargs,
            "agent_name": self.agent_name,
        }
        context = self.context_provider() if self.context_provider is not None else None
        decision = self.before_action(action, context)
        if isawaitable(decision):
            await decision

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the wrapped tool synchronously with security checks."""
        self._authorize()
        if self.before_action is not None:
            raise RuntimeError(
                "before_action requires async tool execution; use ainvoke/astream_events"
            )
        return self._invoke_wrapped_sync(*args, **kwargs)

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the wrapped tool asynchronously with security checks."""
        self._authorize()
        await self._check_before_action(kwargs)
        return await self._invoke_wrapped_async(*args, **kwargs)


def _wrap_tool(
    tool_spec: BaseTool,
    *,
    policy: AgentPolicy | None = None,
    before_action: BeforeActionHook | None = None,
    context_provider: ContextProvider | None = None,
    agent_name: str | None = None,
) -> BaseTool:
    """Return one guarded tool while preserving model-visible metadata."""
    if isinstance(tool_spec, GuardedTool):
        guarded = GuardedTool(
            name=tool_spec.name,
            description=tool_spec.description,
            args_schema=tool_spec.args_schema,
            return_direct=tool_spec.return_direct,
            verbose=tool_spec.verbose,
            callbacks=tool_spec.callbacks,
            tags=tool_spec.tags,
            metadata=tool_spec.metadata,
            handle_tool_error=tool_spec.handle_tool_error,
            handle_validation_error=tool_spec.handle_validation_error,
            response_format=tool_spec.response_format,
            extras=tool_spec.extras,
            wrapped_tool=tool_spec.wrapped_tool,
            policy=policy or tool_spec.policy,
            before_action=before_action or tool_spec.before_action,
            context_provider=context_provider or tool_spec.context_provider,
            agent_name=agent_name or tool_spec.agent_name,
        )
        return _copy_tool_metadata(tool_spec, guarded)

    guarded = GuardedTool(
        name=tool_spec.name,
        description=tool_spec.description,
        args_schema=tool_spec.args_schema,
        return_direct=tool_spec.return_direct,
        verbose=tool_spec.verbose,
        callbacks=tool_spec.callbacks,
        tags=tool_spec.tags,
        metadata=tool_spec.metadata,
        handle_tool_error=tool_spec.handle_tool_error,
        handle_validation_error=tool_spec.handle_validation_error,
        response_format=tool_spec.response_format,
        extras=tool_spec.extras,
        wrapped_tool=tool_spec,
        policy=policy,
        before_action=before_action,
        context_provider=context_provider,
        agent_name=agent_name,
    )
    return _copy_tool_metadata(tool_spec, guarded)


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
        wrapped_tools.append(_wrap_tool(tool_spec, policy=policy))
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
        wrapped_tools.append(
            _wrap_tool(
                tool_spec,
                before_action=before_action,
                context_provider=context_provider,
                agent_name=agent_name,
            )
        )
    return wrapped_tools


def with_before_action(
    agent: AgentGraph,
    before_action: BeforeActionHook,
    *,
    context_provider: ContextProvider | None = None,
) -> AgentGraph:
    """Return an agent runtime with a Gate 2 pre-tool hook applied."""
    return agent.with_before_action(before_action, context_provider=context_provider)
