"""LangChain adapter for :class:`PolicyEnforcer`.

:class:`GuardedTool` wraps a ``BaseTool`` (used by
:meth:`FortifyAgent.enforce_policy`, which rebuilds the graph) and
carries an optional ``approval_handler`` for inline ``NEEDS_APPROVAL``
resolution.
:func:`install_enforcer_on_tool` mutates ``StructuredTool``'s ``func``/
``coroutine`` in place (used by :func:`wrap_langchain_agent` for
pre-built ``CompiledStateGraph``s) and always renders non-allow as a
structured error — approval flows wire in on the host side.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any

from langchain_core.tools import BaseTool
from langchain_core.tools.structured import StructuredTool
from pydantic import ConfigDict

from fortify.agents.factory import (
    ActionContext,
    ActionPayload,
    ApprovalHandler,
    BeforeActionHook,
    ContextProvider,
)
from fortify.security.decision import DecisionOutcome
from fortify.security.enforcer import PolicyEnforcer
from fortify.tools.decorators import TOOL_METADATA_ATTR


def _copy_tool_metadata(source: Any, target: Any) -> Any:
    """Copy fortify tool metadata (tracing labels, etc.) onto a wrapper."""
    metadata = getattr(source, TOOL_METADATA_ATTR, None)
    if metadata is not None:
        setattr(target, TOOL_METADATA_ATTR, metadata)
    return target


def _resolve_approval_sync(
    handler: ApprovalHandler, action: ActionPayload, context: ActionContext
) -> bool:
    """Resolve a NEEDS_APPROVAL decision in a sync caller (rejects coroutines)."""
    if isinstance(handler, bool):
        return handler
    result = handler(action, context)
    if isawaitable(result):
        raise RuntimeError(
            "approval_handler returned a coroutine; sync tool invocation cannot "
            "await it — use ainvoke/astream/astream_events"
        )
    return bool(result)


async def _resolve_approval_async(
    handler: ApprovalHandler, action: ActionPayload, context: ActionContext
) -> bool:
    """Resolve a NEEDS_APPROVAL decision in an async caller."""
    if isinstance(handler, bool):
        return handler
    result = handler(action, context)
    if isawaitable(result):
        result = await result
    return bool(result)


class GuardedTool(BaseTool):
    """LangChain tool wrapper that consults a :class:`PolicyEnforcer`.

    ALLOW delegates to the wrapped tool; non-ALLOW renders
    ``Decision.as_error_payload()`` so the LLM sees governance failures
    as tool output. NEEDS_APPROVAL is treated as denial unless
    ``approval_handler`` (callable or ``bool`` shorthand) returns truthy.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    wrapped_tool: BaseTool
    enforcer: PolicyEnforcer | None = None
    approval_handler: ApprovalHandler | None = None
    # Gate 2: hosted pre-tool hook. Runs after Gate 1 ALLOW (or no enforcer)
    # and before the wrapped tool. Raising any exception inside vetoes the
    # call with a ``before_action_denied`` structured error.
    before_action: BeforeActionHook | None = None
    context_provider: ContextProvider | None = None
    agent_name: str | None = None

    @classmethod
    def wrap(
        cls,
        tool: BaseTool,
        *,
        enforcer: PolicyEnforcer | None = None,
        approval_handler: ApprovalHandler | None = None,
        before_action: BeforeActionHook | None = None,
        context_provider: ContextProvider | None = None,
        agent_name: str | None = None,
    ) -> "GuardedTool":
        """Return a GuardedTool delegating to ``tool`` after policy check.

        Idempotent re-wrap: an existing ``GuardedTool`` is unwrapped once
        so enforcers don't stack; hook fields fall through unless
        explicitly overridden (so chaining ``.with_before_action()`` after
        ``.enforce_policy(...)`` preserves the enforcer).
        """
        if isinstance(tool, cls):
            inner = tool.wrapped_tool
            resolved_enforcer = enforcer if enforcer is not None else tool.enforcer
            resolved_approval = (
                approval_handler if approval_handler is not None else tool.approval_handler
            )
            resolved_before = (
                before_action if before_action is not None else tool.before_action
            )
            resolved_context = (
                context_provider
                if context_provider is not None
                else tool.context_provider
            )
            resolved_agent_name = agent_name or tool.agent_name
        else:
            inner = tool
            resolved_enforcer = enforcer
            resolved_approval = approval_handler
            resolved_before = before_action
            resolved_context = context_provider
            resolved_agent_name = agent_name

        guarded = cls(
            name=inner.name,
            description=inner.description,
            args_schema=inner.args_schema,
            return_direct=inner.return_direct,
            verbose=inner.verbose,
            callbacks=inner.callbacks,
            tags=inner.tags,
            metadata=inner.metadata,
            handle_tool_error=inner.handle_tool_error,
            handle_validation_error=inner.handle_validation_error,
            response_format=inner.response_format,
            extras=inner.extras,
            wrapped_tool=inner,
            enforcer=resolved_enforcer,
            approval_handler=resolved_approval,
            before_action=resolved_before,
            context_provider=resolved_context,
            agent_name=resolved_agent_name,
        )
        return _copy_tool_metadata(inner, guarded)

    def _build_action(self, kwargs: dict[str, Any]) -> ActionPayload:
        """Legacy-shaped action payload for a Gate 2 hook."""
        return {
            "tool_name": self.name,
            "arguments": kwargs,
            "agent_name": self.agent_name,
        }

    def _build_context(self) -> ActionContext:
        """Host context for the Gate 2 hook (None when no provider configured)."""
        return self.context_provider() if self.context_provider is not None else None

    def _before_action_denied(self, message: str) -> dict[str, Any]:
        """Structured Gate 2 veto payload."""
        return {
            "ok": False,
            "error": {
                "type": "before_action_denied",
                "message": message,
                "tool_name": self.name,
                "retryable": False,
            },
        }

    async def _check_before_action(
        self, kwargs: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply the Gate 2 hook; return a veto payload on raise."""
        if self.before_action is None:
            return None
        try:
            result = self.before_action(
                self._build_action(kwargs), self._build_context()
            )
            if isawaitable(result):
                await result
        except Exception as error:
            return self._before_action_denied(str(error))
        return None

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        if self.enforcer is not None:
            decision = self.enforcer.decide(self.name, kwargs)
            if not decision.allowed:
                if (
                    decision.outcome is DecisionOutcome.NEEDS_APPROVAL
                    and self.approval_handler is not None
                    and await _resolve_approval_async(
                        self.approval_handler,
                        self._build_action(kwargs),
                        self._build_context(),
                    )
                ):
                    pass  # approved → fall through to Gate 2 and invoke
                else:
                    return {"ok": False, "error": decision.as_error_payload()}
        veto = await self._check_before_action(kwargs)
        if veto is not None:
            return veto
        return await self._invoke_wrapped_async(*args, **kwargs)

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        if self.enforcer is not None:
            decision = self.enforcer.decide(self.name, kwargs)
            if not decision.allowed:
                if (
                    decision.outcome is DecisionOutcome.NEEDS_APPROVAL
                    and self.approval_handler is not None
                    and _resolve_approval_sync(
                        self.approval_handler,
                        self._build_action(kwargs),
                        self._build_context(),
                    )
                ):
                    pass
                else:
                    return {"ok": False, "error": decision.as_error_payload()}
        if self.before_action is not None:
            raise RuntimeError(
                "before_action requires async tool execution; use ainvoke/astream_events"
            )
        return self._invoke_wrapped_sync(*args, **kwargs)

    async def _invoke_wrapped_async(self, *args: Any, **kwargs: Any) -> Any:
        """Call the wrapped tool without re-entering LangChain instrumentation."""
        if isinstance(self.wrapped_tool, StructuredTool):
            if self.wrapped_tool.coroutine is not None:
                return await self.wrapped_tool.coroutine(*args, **kwargs)
            if self.wrapped_tool.func is not None:
                return self.wrapped_tool.func(*args, **kwargs)
        return await self.wrapped_tool._arun(*args, **kwargs)

    def _invoke_wrapped_sync(self, *args: Any, **kwargs: Any) -> Any:
        if (
            isinstance(self.wrapped_tool, StructuredTool)
            and self.wrapped_tool.func is not None
        ):
            return self.wrapped_tool.func(*args, **kwargs)
        return self.wrapped_tool._run(*args, **kwargs)


# ---------------------------------------------------------------------------
# In-place installer for retrofitting existing CompiledStateGraph tools.
# ---------------------------------------------------------------------------

_ORIGINAL_FUNC_ATTR = "_fortify_original_func"
_ORIGINAL_COROUTINE_ATTR = "_fortify_original_coroutine"
_INSTALLED_ATTR = "_fortify_enforcer_installed"


def install_enforcer_on_tool(
    tool: BaseTool,
    *,
    enforcer: PolicyEnforcer,
) -> BaseTool:
    """Install :class:`PolicyEnforcer` gating on ``tool`` in place.

    Same semantics as :class:`GuardedTool` but mutates ``StructuredTool``'s
    ``func``/``coroutine`` instead of constructing a wrapper — use when
    the tool is already bound to a ``CompiledStateGraph``. Idempotent:
    re-install restores captured originals first so gates don't stack.
    Non-allow outcomes render as the structured error dict; approval
    flows belong on the host side, not on this in-place installer.
    """
    name = tool.name
    original_func: Callable[..., Any] | None = getattr(tool, _ORIGINAL_FUNC_ATTR, None)
    if original_func is None:
        original_func = getattr(tool, "func", None)
    original_coroutine: Callable[..., Awaitable[Any]] | None = getattr(
        tool, _ORIGINAL_COROUTINE_ATTR, None
    )
    if original_coroutine is None:
        original_coroutine = getattr(tool, "coroutine", None)

    if original_func is None and original_coroutine is None:
        raise TypeError(
            f"Cannot install policy on tool {name!r}: it is a "
            f"{type(tool).__name__} without `func`/`coroutine` attributes. "
            "In-place wrapping only supports StructuredTool-style tools."
        )

    if original_func is not None:
        captured_func = original_func

        @functools.wraps(captured_func)
        def guarded_func(*args: Any, **kwargs: Any) -> Any:
            decision = enforcer.decide(name, kwargs)
            if decision.allowed:
                return captured_func(*args, **kwargs)
            return {"ok": False, "error": decision.as_error_payload()}

        setattr(tool, _ORIGINAL_FUNC_ATTR, captured_func)
        tool.func = guarded_func

    if original_coroutine is not None:
        captured_coroutine = original_coroutine

        @functools.wraps(captured_coroutine)
        async def guarded_coroutine(*args: Any, **kwargs: Any) -> Any:
            decision = enforcer.decide(name, kwargs)
            if decision.allowed:
                return await captured_coroutine(*args, **kwargs)
            return {"ok": False, "error": decision.as_error_payload()}

        setattr(tool, _ORIGINAL_COROUTINE_ATTR, captured_coroutine)
        tool.coroutine = guarded_coroutine

    tool.handle_tool_error = True
    setattr(tool, _INSTALLED_ATTR, True)
    return tool


def install_enforcer_on_tools(
    tools: list[BaseTool],
    *,
    enforcer: PolicyEnforcer,
) -> list[BaseTool]:
    """Install enforcement on every StructuredTool-style tool in place."""
    for t in tools:
        install_enforcer_on_tool(t, enforcer=enforcer)
    return tools
