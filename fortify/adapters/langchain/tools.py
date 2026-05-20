from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from inspect import isawaitable
from typing import Any, Iterator, Union

from langchain_core.tools import BaseTool, ToolException
from langchain_core.tools.structured import StructuredTool
from pydantic import ConfigDict

from fortify.security import (
    AgentPolicy,
    ApprovalRequiredError,
    PolicyDeniedError,
    authorize_tool_call,
)
from fortify.security.decision import Decision, DecisionOutcome
from fortify.security.enforcer import PolicyEnforcer
from fortify.tools.decorators import TOOL_METADATA_ATTR


_active_policy: ContextVar[AgentPolicy | None] = ContextVar(
    "fortify_active_policy", default=None
)

_FORTIFY_WRAPPED_ATTR = "_fortify_wrapped"


@contextmanager
def active_policy(policy: AgentPolicy) -> Iterator[None]:
    """Bind `policy` for the current async/thread context.

    Tool gates installed by `wrap_tool` consult this contextvar at call
    time, so callers must enter this context manager before invoking the
    agent. `ContextVar` is per-task, so concurrent invocations for
    different users do not see each other's policies.
    """
    token = _active_policy.set(policy)
    try:
        yield
    finally:
        _active_policy.reset(token)


class ToolDeniedError(ToolException):
    """Raised when a tool call is blocked by the `AgentPolicy`.

    Inherits from `ToolException` so that `BaseTool.run` catches it
    (when `handle_tool_error=True` on the tool) and turns the denial
    message into tool output content, rather than letting it bubble up
    and abort the graph.
    """

    def __init__(self, tool_name: str, reason: str | None = None) -> None:
        self.tool_name = tool_name
        suffix = f" ({reason})" if reason else ""
        message = (
            f"Tool '{tool_name}' is denied by the agent policy {suffix}. "
            "The tool was not executed."
        )
        super().__init__(message)


def wrap_tool(tool: BaseTool) -> BaseTool:
    """Install a contextvar-driven policy gate on `tool` in place.

    Returns the same object so call sites can keep chaining. Idempotent:
    a tool that has already been wrapped is returned untouched.
    """
    if getattr(tool, _FORTIFY_WRAPPED_ATTR, False):
        return tool

    name = tool.name
    original_func = getattr(tool, "func", None)
    original_coroutine = getattr(tool, "coroutine", None)

    if original_func is None and original_coroutine is None:
        raise TypeError(
            f"Cannot install policy on tool {name!r}: it is a "
            f"{type(tool).__name__} without `func`/`coroutine` attributes. "
            "In-place wrapping only supports StructuredTool-style tools."
        )

    if original_func is not None:

        @functools.wraps(original_func)
        def guarded_func(*args: Any, **kwargs: Any) -> Any:
            policy = _active_policy.get()
            if policy is None:
                raise ToolDeniedError(name, "no active Fortify policy")
            try:
                authorize_tool_call(policy, name, kwargs)
            except (PolicyDeniedError, ApprovalRequiredError):
                raise ToolDeniedError(name)
            return original_func(*args, **kwargs)

        tool.func = guarded_func

    if original_coroutine is not None:

        @functools.wraps(original_coroutine)
        async def guarded_coroutine(*args: Any, **kwargs: Any) -> Any:
            policy = _active_policy.get()
            if policy is None:
                raise ToolDeniedError(name, "no active Fortify policy")
            try:
                authorize_tool_call(policy, name, kwargs)
            except (PolicyDeniedError, ApprovalRequiredError):
                raise ToolDeniedError(name)
            return await original_coroutine(*args, **kwargs)

        tool.coroutine = guarded_coroutine

    tool.handle_tool_error = True
    setattr(tool, _FORTIFY_WRAPPED_ATTR, True)
    return tool


def wrap_tools(tools: list[BaseTool]) -> list[BaseTool]:
    """Install policy gates on `tools` in place, returning the same list."""
    for t in tools:
        wrap_tool(t)
    return tools


# ---------------------------------------------------------------------------
# GuardedTool — PolicyEnforcer-based wrapper (the new path).
#
# Coexists with the legacy in-place ``wrap_tool`` / ``ToolDeniedError`` above
# during the migration. Step 1 of the refactor: this class is reachable but
# not wired into any production call site yet. ``AgentGraph.enforce_policy``
# and ``FortifyLangchainAgent`` still use the legacy machinery; they get
# switched over in later steps.
# ---------------------------------------------------------------------------

# Optional adapter-level callback for resolving NEEDS_APPROVAL decisions.
# ``bool`` shorthand: True = always approve, False = always deny — matches the
# CLI's --approval-mode=auto-approve / auto-deny semantics.
ApprovalHandler = Union[
    Callable[[Decision], "bool | Awaitable[bool]"],
    bool,
]


def _copy_tool_metadata(source: Any, target: Any) -> Any:
    """Copy fortify-specific tool metadata (e.g. tracing labels) onto the wrapper."""
    metadata = getattr(source, TOOL_METADATA_ATTR, None)
    if metadata is not None:
        setattr(target, TOOL_METADATA_ATTR, metadata)
    return target


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
    enforcer: PolicyEnforcer
    approval_handler: ApprovalHandler | None = None

    @classmethod
    def wrap(
        cls,
        tool: BaseTool,
        *,
        enforcer: PolicyEnforcer,
        approval_handler: ApprovalHandler | None = None,
    ) -> "GuardedTool":
        """Return a GuardedTool that delegates to ``tool`` after policy check.

        Idempotent on re-wrap: if ``tool`` is already a ``GuardedTool``,
        the inner wrapped_tool is unwrapped first so we don't stack
        enforcers. The new enforcer and approval_handler replace whatever
        the previous wrapper carried.
        """
        inner = tool.wrapped_tool if isinstance(tool, cls) else tool
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
            enforcer=enforcer,
            approval_handler=approval_handler,
        )
        return _copy_tool_metadata(inner, guarded)

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        decision = self.enforcer.decide(self.name, kwargs)
        if decision.allowed:
            return await self._invoke_wrapped_async(*args, **kwargs)
        if (
            decision.outcome is DecisionOutcome.NEEDS_APPROVAL
            and self.approval_handler is not None
            and await self._resolve_approval_async(decision)
        ):
            return await self._invoke_wrapped_async(*args, **kwargs)
        return {"ok": False, "error": decision.as_error_payload()}

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        decision = self.enforcer.decide(self.name, kwargs)
        if decision.allowed:
            return self._invoke_wrapped_sync(*args, **kwargs)
        if (
            decision.outcome is DecisionOutcome.NEEDS_APPROVAL
            and self.approval_handler is not None
            and self._resolve_approval_sync(decision)
        ):
            return self._invoke_wrapped_sync(*args, **kwargs)
        return {"ok": False, "error": decision.as_error_payload()}

    async def _resolve_approval_async(self, decision: Decision) -> bool:
        if isinstance(self.approval_handler, bool):
            return self.approval_handler
        result = self.approval_handler(decision)
        if isawaitable(result):
            result = await result
        return bool(result)

    def _resolve_approval_sync(self, decision: Decision) -> bool:
        if isinstance(self.approval_handler, bool):
            return self.approval_handler
        result = self.approval_handler(decision)
        if isawaitable(result):
            raise RuntimeError(
                "approval_handler returned a coroutine; sync tool invocation "
                "cannot await it — use ainvoke/astream_events"
            )
        return bool(result)

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
