"""Apply security concerns to created agent runtimes."""

from __future__ import annotations

from collections.abc import Sequence
from inspect import isawaitable
from typing import Any

from langchain_core.tools import BaseTool
from langchain_core.tools.structured import StructuredTool
from pydantic import ConfigDict

from fortify.agents.factory import (
    ActionContext,
    ActionPayload,
    AgentGraph,
    ApprovalHandler,
    BeforeActionHook,
    ContextProvider,
    ToolSpec,
)
from fortify.security import (
    AgentPolicy,
    ApprovalRequiredError,
    FileToolPolicy,
    PolicyDeniedError,
)
from fortify.security.file_scope import build_file_scope_hint
from fortify.security.policy_set import PolicySet, load_policy_set
from fortify.tools.decorators import TOOL_METADATA_ATTR


def _copy_tool_metadata(source: Any, target: Any) -> Any:
    """Copy fortify-specific metadata from one tool object to another."""
    metadata = getattr(source, TOOL_METADATA_ATTR, None)
    if metadata is not None:
        setattr(target, TOOL_METADATA_ATTR, metadata)
    return target


class GuardedTool(BaseTool):
    """A tool wrapper that enforces local policy and hosted hooks inline.

    ``policy_set`` is a role-aware bundle; at call time the active
    :class:`~fortify.runtime.User.role` selects which policy applies. When
    no User scope is active the bundle's ``default`` role is used.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    wrapped_tool: BaseTool
    policy_set: PolicySet | None = None
    approval_handler: ApprovalHandler | None = None
    before_action: BeforeActionHook | None = None
    context_provider: ContextProvider | None = None
    agent_name: str | None = None

    def _active_policy(self) -> AgentPolicy | None:
        """Resolve the effective :class:`AgentPolicy` for the current User role."""
        if self.policy_set is None:
            return None
        from fortify.runtime.context import get_current_user

        active_user = get_current_user()
        role = active_user.role if active_user is not None else None
        return self.policy_set.policy_for(role)

    def _build_action(self, kwargs: dict[str, Any]) -> ActionPayload:
        """Build a host-facing action payload for approval and veto hooks."""
        return {
            "tool_name": self.name,
            "arguments": kwargs,
            "agent_name": self.agent_name,
        }

    def _build_context(self) -> ActionContext:
        """Return the current host context for approval and veto hooks."""
        return self.context_provider() if self.context_provider is not None else None

    def _security_result(self, error_type: str, message: str) -> dict[str, Any]:
        """Return a structured governance failure payload."""
        # TODO: evolve this into a richer interruption/decision shape for UI pause-resume flows.
        error_payload: dict[str, Any] = {
            "type": error_type,
            "message": message,
            "tool_name": self.name,
            "retryable": False,
        }
        active_policy = self._active_policy()
        if active_policy is not None:
            tool_policy = active_policy.tools.get(self.name)
            if isinstance(tool_policy, FileToolPolicy):
                hint = build_file_scope_hint(tool_policy)
                if hint is not None:
                    error_payload["hint"] = hint
        return {
            "ok": False,
            "error": error_payload,
        }

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
        if (
            isinstance(self.wrapped_tool, StructuredTool)
            and self.wrapped_tool.func is not None
        ):
            return self.wrapped_tool.func(*args, **kwargs)
        return self.wrapped_tool._run(*args, **kwargs)

    def _authorize(self, kwargs: dict[str, Any]) -> None:
        """Apply local Gate 1 authorization when configured.

        Picks the effective :class:`AgentPolicy` from ``policy_set`` based
        on the active :class:`~fortify.runtime.User.role`, then runs the
        tool's ``mode`` + ``constraints`` check against the invocation
        arguments. When no User scope is active the ``default`` role
        applies — keeps local-only flows working unchanged.
        """
        active_policy = self._active_policy()
        if active_policy is None:
            return
        from fortify.security import authorize_tool_call

        authorize_tool_call(active_policy, self.name, kwargs)

    async def _check_before_action(
        self, kwargs: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply the hosted Gate 2 hook when configured."""
        if self.before_action is None:
            return None
        try:
            decision = self.before_action(
                self._build_action(kwargs), self._build_context()
            )
            if isawaitable(decision):
                await decision
        except Exception as error:
            return self._security_result("before_action_denied", str(error))
        return None

    async def _resolve_approval_async(self, kwargs: dict[str, Any]) -> bool:
        """Return whether an approval-required action has been approved."""
        if self.approval_handler is None:
            return False
        if isinstance(self.approval_handler, bool):
            return self.approval_handler
        decision = self.approval_handler(
            self._build_action(kwargs), self._build_context()
        )
        if isawaitable(decision):
            decision = await decision
        return bool(decision)

    def _resolve_approval_sync(self, kwargs: dict[str, Any]) -> bool:
        """Return whether an approval-required action has been approved."""
        if self.approval_handler is None:
            return False
        if isinstance(self.approval_handler, bool):
            return self.approval_handler
        decision = self.approval_handler(
            self._build_action(kwargs), self._build_context()
        )
        if isawaitable(decision):
            raise RuntimeError(
                "approval_handler requires async tool execution when it returns an awaitable"
            )
        return bool(decision)

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the wrapped tool synchronously with security checks."""
        try:
            self._authorize(kwargs)
        except PolicyDeniedError as error:
            return self._security_result("policy_denied", str(error))
        except ApprovalRequiredError:
            if not self._resolve_approval_sync(kwargs):
                return self._security_result(
                    "approval_required",
                    f'Tool "{self.name}" requires approval before execution',
                )
        if self.before_action is not None:
            raise RuntimeError(
                "before_action requires async tool execution; use ainvoke/astream_events"
            )
        return self._invoke_wrapped_sync(*args, **kwargs)

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the wrapped tool asynchronously with security checks."""
        try:
            self._authorize(kwargs)
        except PolicyDeniedError as error:
            return self._security_result("policy_denied", str(error))
        except ApprovalRequiredError:
            if not await self._resolve_approval_async(kwargs):
                return self._security_result(
                    "approval_required",
                    f'Tool "{self.name}" requires approval before execution',
                )
        before_action_result = await self._check_before_action(kwargs)
        if before_action_result is not None:
            return before_action_result
        return await self._invoke_wrapped_async(*args, **kwargs)


def _wrap_tool(
    tool_spec: BaseTool,
    *,
    policy_set: PolicySet | None = None,
    approval_handler: ApprovalHandler | None = None,
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
            policy_set=policy_set or tool_spec.policy_set,
            approval_handler=approval_handler
            if approval_handler is not None
            else tool_spec.approval_handler,
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
        policy_set=policy_set,
        approval_handler=approval_handler,
        before_action=before_action,
        context_provider=context_provider,
        agent_name=agent_name,
    )
    return _copy_tool_metadata(tool_spec, guarded)


def wrap_tools_with_policy(
    tools: Sequence[ToolSpec],
    policy: AgentPolicy | PolicySet | None,
) -> list[ToolSpec]:
    """Wrap tools so policy decisions are enforced before execution.

    ``policy`` may be a legacy single :class:`AgentPolicy` (which becomes
    a one-role ``default`` :class:`PolicySet`) or an already-built
    :class:`PolicySet` with multiple roles.
    """
    if policy is None:
        return list(tools)
    policy_set = (
        policy if isinstance(policy, PolicySet) else load_policy_set(policy)
    )

    wrapped_tools: list[ToolSpec] = []
    for tool_spec in tools:
        if not isinstance(tool_spec, BaseTool):
            wrapped_tools.append(tool_spec)
            continue
        wrapped_tools.append(_wrap_tool(tool_spec, policy_set=policy_set))
    return wrapped_tools


def enforce_policy(
    agent: AgentGraph,
    policy: str | AgentPolicy | PolicySet | None,
) -> AgentGraph:
    """Return an agent runtime with Gate 1 policy enforcement applied.

    ``policy`` may be a path to a single YAML file (legacy), a directory of
    role policies (the new shape), an :class:`AgentPolicy` model, or a
    pre-built :class:`PolicySet`.
    """
    return agent.enforce_policy(policy)


def wrap_tools_with_approval_handler(
    tools: Sequence[ToolSpec],
    approval_handler: ApprovalHandler,
    *,
    context_provider: ContextProvider | None = None,
    agent_name: str | None = None,
) -> list[ToolSpec]:
    """Wrap tools so approval-required policy outcomes can consult a host handler."""
    wrapped_tools: list[ToolSpec] = []
    for tool_spec in tools:
        if not isinstance(tool_spec, BaseTool):
            wrapped_tools.append(tool_spec)
            continue
        wrapped_tools.append(
            _wrap_tool(
                tool_spec,
                approval_handler=approval_handler,
                context_provider=context_provider,
                agent_name=agent_name,
            )
        )
    return wrapped_tools


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


def with_approval_handler(
    agent: AgentGraph,
    approval_handler: ApprovalHandler,
    *,
    context_provider: ContextProvider | None = None,
) -> AgentGraph:
    """Return an agent runtime with a Gate 1 approval resolver applied."""
    return agent.with_approval_handler(
        approval_handler, context_provider=context_provider
    )
