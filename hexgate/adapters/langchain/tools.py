"""LangChain adapter for :class:`PolicyEnforcer`.

:class:`GuardedTool` wraps a ``BaseTool`` (used by
:meth:`HexgateAgent.enforce_policy`, which rebuilds the graph) and
carries an optional ``approval_handler`` for inline ``NEEDS_APPROVAL``
resolution.
:func:`install_enforcer_on_tool` mutates ``StructuredTool``'s ``func``/
``coroutine`` in place (used by :func:`wrap_langchain_agent` for
pre-built ``CompiledStateGraph``s) and always renders non-allow as a
structured error — approval flows wire in on the host side.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.tools import BaseTool
from langchain_core.tools.structured import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from hexgate.approvals import ApprovalHandler
from hexgate.guards.runner import run_guarded_async, run_guarded_sync
from hexgate.guards.types import ToolPipeline
from hexgate.security.decision import Decision
from hexgate.security.enforcer import PolicyEnforcer
from hexgate.tools.decorators import TOOL_METADATA_ATTR


def _copy_tool_metadata(source: Any, target: Any) -> Any:
    """Copy hexgate tool metadata (tracing labels, etc.) onto a wrapper."""
    metadata = getattr(source, TOOL_METADATA_ATTR, None)
    if metadata is not None:
        setattr(target, TOOL_METADATA_ATTR, metadata)
    return target


def _langchain_error(decision: Decision) -> dict[str, Any]:
    """Render a blocked decision as the LangChain tool-result error dict.

    The shared runner shapes every non-allow (policy deny, approval-required,
    or a guard ``Halt``) into a :class:`Decision` and hands it here, so the LLM
    sees governance failures as ``{"ok": False, ...}`` tool output.
    """
    return {"ok": False, "error": decision.as_error_payload()}


def _content_text(content: Any) -> str:
    """Coerce a message's ``content`` to text.

    A LangChain message content may be a plain string or a list of content blocks
    (e.g. Anthropic multimodal / structured output); flatten the latter to the
    text parts so the parent LLM gets a string, not a raw block list.
    """
    if content is None:
        return ""  # a tool-call-only / empty completion → no text, not the word "None"
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


def _stringify(value: Any) -> str:
    """Coerce a structured / ``response_format`` payload to a string.

    A LangGraph ``ToolNode`` puts the tool's return into a ``ToolMessage`` whose
    content must be text, so a raw dict/model would be ``str()``-ified to an ugly
    Python repr. Prefer JSON (a pydantic model via ``model_dump_json``) so the parent
    LLM sees clean, parseable text; fall back to ``str`` for the non-serializable."""
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _final_message_text(result: Any) -> str:
    """Extract the child's final answer from an ``ainvoke`` result, as a string.

    A ``response_format`` child returns a LangGraph state with BOTH ``messages`` and
    ``structured_response``; the structured answer is the real payload, so prefer it
    over the last message (serialized to text). Otherwise handle a plain string, a
    ``{"messages": [...]}`` state (the last message may be a ``BaseMessage`` or a
    plain dict), or any other shape — always returned as a string, never a raw dict
    (a ToolNode requires string tool-message content) and never silently dropped.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if result.get("structured_response") is not None:
            return _stringify(result["structured_response"])
        if "messages" in result:
            # An agent-state result: empty messages → empty answer, not str(state).
            messages = result["messages"]
            if not messages:
                return ""
            last = messages[-1]
            content = (
                last.get("content")
                if isinstance(last, dict)
                else getattr(last, "content", last)
            )
            return _content_text(content)
    return str(result)  # any other shape — convey as text, don't drop


class GuardedTool(BaseTool):
    """LangChain tool wrapper that consults a :class:`PolicyEnforcer`.

    ALLOW delegates to the wrapped tool; non-ALLOW renders
    ``Decision.as_error_payload()`` so the LLM sees governance failures
    as tool output. NEEDS_APPROVAL is treated as denial unless
    ``approval_handler`` (callable taking the :class:`Decision`, or a
    ``bool`` shorthand) returns truthy.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    wrapped_tool: BaseTool
    enforcer: PolicyEnforcer | None = None
    approval_handler: ApprovalHandler | None = None
    pipeline: ToolPipeline | None = None

    @classmethod
    def wrap(
        cls,
        tool: BaseTool,
        *,
        enforcer: PolicyEnforcer | None = None,
        approval_handler: ApprovalHandler | None = None,
        pipeline: ToolPipeline | None = None,
    ) -> "GuardedTool":
        """Return a GuardedTool delegating to ``tool`` after policy check.

        Idempotent re-wrap: an existing ``GuardedTool`` is unwrapped once
        so enforcers don't stack; fields fall through unless explicitly
        overridden.
        """
        if isinstance(tool, cls):
            inner = tool.wrapped_tool
            resolved_enforcer = enforcer if enforcer is not None else tool.enforcer
            resolved_approval = (
                approval_handler
                if approval_handler is not None
                else tool.approval_handler
            )
            resolved_pipeline = pipeline if pipeline is not None else tool.pipeline
        else:
            inner = tool
            resolved_enforcer = enforcer
            resolved_approval = approval_handler
            resolved_pipeline = pipeline

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
            pipeline=resolved_pipeline,
        )
        return _copy_tool_metadata(inner, guarded)

    def _guarded(self) -> bool:
        """True when this tool has anything to run: an enforcer or guards."""
        return self.enforcer is not None or (
            self.pipeline is not None and not self.pipeline.is_empty
        )

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        if not self._guarded():
            return await self._invoke_wrapped_async(*args, **kwargs)
        return await run_guarded_async(
            self.name,
            kwargs,
            enforcer=self.enforcer,
            pipeline=self.pipeline,
            approval_handler=self.approval_handler,
            invoke=lambda final: self._invoke_wrapped_async(*args, **final),
            render_error=_langchain_error,
        )

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        if not self._guarded():
            return self._invoke_wrapped_sync(*args, **kwargs)
        return run_guarded_sync(
            self.name,
            kwargs,
            enforcer=self.enforcer,
            pipeline=self.pipeline,
            approval_handler=self.approval_handler,
            invoke=lambda final: self._invoke_wrapped_sync(*args, **final),
            render_error=_langchain_error,
        )

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
# Sub-agent delegation (agent-as-tool) seam.
# ---------------------------------------------------------------------------


class _SubagentInput(BaseModel):
    """The single free-text argument the LLM passes to a sub-agent."""

    task: str = Field(
        description="The task or request to delegate to the sub-agent, in plain text."
    )


class SubagentTool(BaseTool):
    """A policy-gated agent-as-tool delegation seam.

    Exposed to the parent's LLM as a normal tool. On call it (1) decides
    ``agent.tool:<target>`` under the PARENT's policy via :class:`ReachGate`
    (a no-op unless the policy declares reach; a denial renders as the structured
    tool error, like :class:`GuardedTool`), then (2) runs ``child.ainvoke`` — which
    enforces the *child's* own policy and inherits the caller's role from the
    ambient :class:`HexgateContext`. Control returns here with the child's answer.

    ``enforcer`` is injected by ``enforce_policy`` (the same object the binding
    holds, so a policy refresh reaches it); until then it is ``None`` and the reach
    gate is skipped (the child still self-enforces).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    args_schema: type[BaseModel] = _SubagentInput
    # ``child`` is a HexgateAgent; typed Any to avoid the factory import cycle.
    child: Any = None
    target_name: str = ""  # the child's name — the ``agent.tool:<target>`` key
    enforcer: PolicyEnforcer | None = None
    approval_handler: ApprovalHandler | None = None
    pipeline: ToolPipeline | None = None

    async def _arun(self, task: str, **_: Any) -> Any:
        # Reach (agent.tool:<child>) is the PARENT policy's gate on the delegation
        # itself, so decide it BEFORE the guard pipeline: a denial short-circuits and
        # never runs the pipeline. This also keeps the runner's success path honest —
        # returning a denial dict as a normal invoke result would be recorded as a
        # successful tool call (ToolOutcome ok=True) by post-guards/observers.
        denial = await self._reach_denial()
        if denial is not None:
            return denial
        if self.pipeline is not None and not self.pipeline.is_empty:
            return await run_guarded_async(
                self.name,
                {"task": task},
                enforcer=None,  # reach already decided above; no tool-name policy here
                pipeline=self.pipeline,
                approval_handler=self.approval_handler,
                invoke=lambda final: self._delegate_child(final.get("task", task)),
                render_error=_langchain_error,
            )
        return await self._delegate_child(task)

    async def _reach_denial(self) -> dict[str, Any] | None:
        """Decide ``agent.tool:<child>`` under the parent policy.

        Returns a rendered tool error on deny, or ``None`` to proceed (allow, no reach
        declared, or an unbound parent whose child self-enforces).
        """
        if self.enforcer is None:
            return None
        from hexgate.security.agent_gate import (
            ReachNotAllowedError,
            resolve_reach_gate,
        )

        gate = resolve_reach_gate(self.enforcer, approval_handler=self.approval_handler)
        try:
            # No-op unless the policy declares reach; raises on deny.
            await gate.check_reach_async(self.target_name, via="tool")
        except ReachNotAllowedError as err:
            return _langchain_error(err.decision)
        return None

    async def _delegate_child(self, task: str) -> Any:
        from hexgate.security.agent_gate import (
            AgentNotAdmittedError,
            ReachNotAllowedError,
        )
        from hexgate.security.bans import AgentBannedError

        # The child enforces its OWN policy (admission + tools) under the inherited
        # role. A refusal there is a governance outcome, so render it as tool output —
        # the same {"ok": False, ...} shape as a reach deny — rather than letting it
        # abort the parent run with a raw exception.
        # NB (PR 1): the parent run config is not threaded into the child, so a
        # sub-agent must not use a checkpointer and its run is not nested in the
        # parent trace — config/trace threading is a follow-up.
        try:
            result = await self.child.ainvoke(
                {"messages": [{"role": "user", "content": task}]}, {}
            )
        except (AgentNotAdmittedError, ReachNotAllowedError) as err:
            return _langchain_error(err.decision)
        except AgentBannedError as err:
            return {"ok": False, "error": {"reason": str(err)}}
        return _final_message_text(result)

    def _run(self, task: str, **_: Any) -> Any:
        # Native sub-agent delegation runs the child asynchronously; LangGraph
        # drives tools via _arun, so the sync path is intentionally unsupported.
        raise NotImplementedError(
            "SubagentTool requires the async invoke path (ainvoke/astream_events)."
        )


# ---------------------------------------------------------------------------
# In-place installer for retrofitting existing CompiledStateGraph tools.
# ---------------------------------------------------------------------------

_ORIGINAL_FUNC_ATTR = "_hexgate_original_func"
_ORIGINAL_COROUTINE_ATTR = "_hexgate_original_coroutine"
_INSTALLED_ATTR = "_hexgate_enforcer_installed"


def install_enforcer_on_tool(
    tool: BaseTool,
    *,
    enforcer: PolicyEnforcer,
    pipeline: ToolPipeline | None = None,
) -> BaseTool:
    """Install :class:`PolicyEnforcer` gating on ``tool`` in place.

    Same semantics as :class:`GuardedTool` but mutates ``StructuredTool``'s
    ``func``/``coroutine`` instead of constructing a wrapper — use when
    the tool is already bound to a ``CompiledStateGraph``. Idempotent:
    re-install restores captured originals first so gates don't stack.
    Non-allow outcomes render as the structured error dict; approval
    flows belong on the host side, not on this in-place installer, so the
    runner runs with ``approval_handler=None``.
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
            return run_guarded_sync(
                name,
                kwargs,
                enforcer=enforcer,
                pipeline=pipeline,
                approval_handler=None,
                invoke=lambda final: captured_func(*args, **final),
                render_error=_langchain_error,
            )

        setattr(tool, _ORIGINAL_FUNC_ATTR, captured_func)
        tool.func = guarded_func

    if original_coroutine is not None:
        captured_coroutine = original_coroutine

        @functools.wraps(captured_coroutine)
        async def guarded_coroutine(*args: Any, **kwargs: Any) -> Any:
            return await run_guarded_async(
                name,
                kwargs,
                enforcer=enforcer,
                pipeline=pipeline,
                approval_handler=None,
                invoke=lambda final: captured_coroutine(*args, **final),
                render_error=_langchain_error,
            )

        setattr(tool, _ORIGINAL_COROUTINE_ATTR, captured_coroutine)
        tool.coroutine = guarded_coroutine

    tool.handle_tool_error = True
    setattr(tool, _INSTALLED_ATTR, True)
    return tool


def install_enforcer_on_tools(
    tools: list[BaseTool],
    *,
    enforcer: PolicyEnforcer,
    pipeline: ToolPipeline | None = None,
) -> list[BaseTool]:
    """Install enforcement on every StructuredTool-style tool in place."""
    for t in tools:
        install_enforcer_on_tool(t, enforcer=enforcer, pipeline=pipeline)
    return tools
