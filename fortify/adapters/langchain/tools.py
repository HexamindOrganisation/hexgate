"""LangChain adapter for :class:`~fortify.security.enforcer.PolicyEnforcer`.

Two ways to install enforcement on a LangChain tool live here:

* :class:`GuardedTool` — wraps a :class:`BaseTool` in a new ``BaseTool``
  subclass. Used by :meth:`FortifyAgent.enforce_policy`, which controls
  its own graph and can swap tools by rebuilding.

* :func:`install_enforcer_on_tool` — mutates a ``StructuredTool``'s
  ``func`` / ``coroutine`` callables in place. Used by
  :func:`wrap_langchain_agent` to retrofit a pre-built
  ``CompiledStateGraph`` whose tool references can't be replaced.

Both paths consult the same :class:`PolicyEnforcer`, render
:class:`Decision` failures the same way, and accept the same optional
``approval_handler`` for inline ``NEEDS_APPROVAL`` resolution.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any, Union

from langchain_core.tools import BaseTool
from langchain_core.tools.structured import StructuredTool
from pydantic import ConfigDict

from fortify.security.decision import Decision, DecisionOutcome
from fortify.security.enforcer import PolicyEnforcer
from fortify.tools.decorators import TOOL_METADATA_ATTR


# Optional adapter-level callback for resolving NEEDS_APPROVAL decisions.
# ``bool`` shorthand: True = always approve, False = always deny — matches
# the CLI's --approval-mode=auto-approve / auto-deny semantics.
ApprovalHandler = Union[
    Callable[[Decision], "bool | Awaitable[bool]"],
    bool,
]


# Gate 2 host-facing types — kept compatible with the ``with_before_action``
# shim, which calls the hook with these dict shapes.
ActionPayload = dict[str, Any]
ActionContext = Union[dict[str, Any], None]
BeforeActionHook = Callable[
    [ActionPayload, ActionContext], "object | Awaitable[object]"
]
ContextProvider = Callable[[], ActionContext]


def _copy_tool_metadata(source: Any, target: Any) -> Any:
    """Copy fortify-specific tool metadata (e.g. tracing labels) onto a wrapper."""
    metadata = getattr(source, TOOL_METADATA_ATTR, None)
    if metadata is not None:
        setattr(target, TOOL_METADATA_ATTR, metadata)
    return target


def _resolve_approval_sync(handler: ApprovalHandler, decision: Decision) -> bool:
    """Resolve a NEEDS_APPROVAL decision against ``handler`` in a sync caller."""
    if isinstance(handler, bool):
        return handler
    result = handler(decision)
    if isawaitable(result):
        raise RuntimeError(
            "approval_handler returned a coroutine; sync tool invocation cannot "
            "await it — use ainvoke/astream/astream_events"
        )
    return bool(result)


async def _resolve_approval_async(handler: ApprovalHandler, decision: Decision) -> bool:
    """Resolve a NEEDS_APPROVAL decision against ``handler`` in an async caller."""
    if isinstance(handler, bool):
        return handler
    result = handler(decision)
    if isawaitable(result):
        result = await result
    return bool(result)


class GuardedTool(BaseTool):
    """LangChain tool wrapper that consults a :class:`PolicyEnforcer`.

    Each invocation asks the enforcer for a :class:`Decision`. Allowed
    calls delegate to the wrapped tool. Denied calls return a structured
    error payload (``Decision.as_error_payload()``) so the LLM sees the
    governance failure as tool output rather than an exception.

    NEEDS_APPROVAL is treated as denial by default. If the adapter is
    constructed with ``approval_handler``, the handler is consulted: it
    may return ``bool``, a coroutine resolving to ``bool``, or be a plain
    ``bool`` shorthand (always-approve / always-deny). A truthy handler
    decision lets the wrapped tool run; a falsy one renders the same
    structured error as denial.
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
        """Return a GuardedTool that delegates to ``tool`` after policy check.

        Idempotent on re-wrap: if ``tool`` is already a ``GuardedTool``,
        the inner wrapped_tool is unwrapped first so we don't stack
        enforcers. Hook fields fall through from the existing wrapper when
        not explicitly overridden — so chaining ``.with_before_action()``
        after ``.enforce_policy(...)`` preserves the enforcer.
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
            wrapped_tool=inner,
            enforcer=resolved_enforcer,
            approval_handler=resolved_approval,
            before_action=resolved_before,
            context_provider=resolved_context,
            agent_name=resolved_agent_name,
        )
        return _copy_tool_metadata(inner, guarded)

    def _build_action(self, kwargs: dict[str, Any]) -> ActionPayload:
        """Build the legacy-shaped action payload for a Gate 2 hook."""
        return {
            "tool_name": self.name,
            "arguments": kwargs,
            "agent_name": self.agent_name,
        }

    def _build_context(self) -> ActionContext:
        """Return the current host context for the Gate 2 hook, when configured."""
        return self.context_provider() if self.context_provider is not None else None

    def _before_action_denied(self, message: str) -> dict[str, Any]:
        """Render a Gate 2 veto as the structured tool failure the LLM sees."""
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
        """Apply the Gate 2 hook; return a structured veto payload on raise."""
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
                    and await _resolve_approval_async(self.approval_handler, decision)
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
                    and _resolve_approval_sync(self.approval_handler, decision)
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
        """Call the wrapped tool's implementation without re-entering instrumentation."""
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
    approval_handler: ApprovalHandler | None = None,
) -> BaseTool:
    """Install :class:`PolicyEnforcer` gating on ``tool`` in place.

    Mirrors :class:`GuardedTool` semantics — call the enforcer, render
    the :class:`Decision` as a structured error on non-allow — but
    mutates the underlying ``StructuredTool``'s ``func`` and
    ``coroutine`` callables rather than constructing a new BaseTool.
    Use when the tool is already bound to a LangGraph
    ``CompiledStateGraph`` and cannot be replaced.

    Idempotent: re-installing on an already-installed tool restores the
    captured originals first so the new enforcer + handler take effect
    without stacking gates.
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
            if (
                decision.outcome is DecisionOutcome.NEEDS_APPROVAL
                and approval_handler is not None
                and _resolve_approval_sync(approval_handler, decision)
            ):
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
            if (
                decision.outcome is DecisionOutcome.NEEDS_APPROVAL
                and approval_handler is not None
                and await _resolve_approval_async(approval_handler, decision)
            ):
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
    approval_handler: ApprovalHandler | None = None,
) -> list[BaseTool]:
    """Install enforcement on every StructuredTool-style tool in place."""
    for t in tools:
        install_enforcer_on_tool(
            t, enforcer=enforcer, approval_handler=approval_handler
        )
    return tools
