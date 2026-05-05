"""Tests for the LangChain adapter policy gate on tools."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import BaseTool, tool

from fortify.adapters.langchain.tools import (
    ToolDeniedError,
    active_policy,
    wrap_tool,
    wrap_tools,
)
from fortify.security import AgentPolicy


def _allow_policy(tool_name: str = "echo") -> AgentPolicy:
    """Build a policy that allows a single named tool, denying everything else."""
    return AgentPolicy.model_validate(
        {
            "default_policy": {"mode": "deny"},
            "tools": {tool_name: {"mode": "allow"}},
        }
    )


def _deny_policy() -> AgentPolicy:
    """Build a policy that denies every tool by default."""
    return AgentPolicy.model_validate({"default_policy": {"mode": "deny"}})


def _approval_required_policy(tool_name: str = "echo") -> AgentPolicy:
    """Build a policy where the named tool requires approval."""
    return AgentPolicy.model_validate(
        {
            "default_policy": {"mode": "deny"},
            "tools": {tool_name: {"mode": "approval_required"}},
        }
    )


def _make_sync_tool(name: str = "echo") -> BaseTool:
    """Create a StructuredTool-style sync tool returning its argument."""

    @tool(name)
    def echo(text: str) -> str:
        """Echo the input back."""
        return f"echo:{text}"

    return echo


def _make_async_tool(name: str = "echo") -> BaseTool:
    """Create a StructuredTool-style async tool returning its argument."""

    @tool(name)
    async def echo(text: str) -> str:
        """Echo the input back asynchronously."""
        return f"async:{text}"

    return echo


def test_tool_denied_error_message_includes_reason() -> None:
    """Format the denial message with the tool name and optional reason."""
    err = ToolDeniedError("read_file", "no active Fortify policy")

    assert err.tool_name == "read_file"
    assert "read_file" in str(err)
    assert "no active Fortify policy" in str(err)
    assert "not executed" in str(err)


def test_tool_denied_error_omits_reason_suffix_when_none() -> None:
    """Render a clean message when no reason is supplied."""
    err = ToolDeniedError("read_file")

    assert err.tool_name == "read_file"
    assert "()" not in str(err)
    assert "read_file" in str(err)


def test_active_policy_sets_and_resets_contextvar() -> None:
    """Bind the policy only inside the context manager."""
    from fortify.adapters.langchain import tools as langchain_tools

    assert langchain_tools._active_policy.get() is None

    policy = _allow_policy()
    with active_policy(policy):
        assert langchain_tools._active_policy.get() is policy

    assert langchain_tools._active_policy.get() is None


def test_active_policy_resets_after_exception() -> None:
    """Reset the contextvar even when the body raises."""
    from fortify.adapters.langchain import tools as langchain_tools

    policy = _allow_policy()
    with pytest.raises(RuntimeError, match="boom"):
        with active_policy(policy):
            assert langchain_tools._active_policy.get() is policy
            raise RuntimeError("boom")

    assert langchain_tools._active_policy.get() is None


def test_wrap_tool_sets_handle_tool_error_and_marks_wrapped() -> None:
    """Mark wrapped tools so `BaseTool.run` converts denials into output."""
    sync_tool = _make_sync_tool()

    wrapped = wrap_tool(sync_tool)

    assert wrapped is sync_tool
    assert wrapped.handle_tool_error is True
    assert getattr(wrapped, "_fortify_wrapped") is True


def test_wrap_tool_is_idempotent() -> None:
    """Skip re-wrapping a tool that has already been wrapped."""
    sync_tool = _make_sync_tool()

    wrap_tool(sync_tool)
    guarded_func = sync_tool.func

    wrap_tool(sync_tool)

    assert sync_tool.func is guarded_func


def test_wrap_tool_rejects_tool_without_func_or_coroutine() -> None:
    """Raise TypeError for tools that are not StructuredTool-compatible."""

    class BareTool(BaseTool):
        name: str = "bare"
        description: str = "tool without func/coroutine"

        def _run(self, *_args: Any, **_kwargs: Any) -> str:
            """Pretend to do work."""
            return "ok"

    bare = BareTool()

    with pytest.raises(TypeError, match="StructuredTool-style"):
        wrap_tool(bare)


def test_sync_tool_denied_when_no_active_policy() -> None:
    """Raise ToolDeniedError when invoked outside an active_policy block."""
    sync_tool = wrap_tool(_make_sync_tool())

    with pytest.raises(ToolDeniedError, match="no active Fortify policy"):
        sync_tool.func(text="hello")


def test_sync_tool_denied_when_policy_denies() -> None:
    """Raise ToolDeniedError when the active policy denies the tool."""
    sync_tool = wrap_tool(_make_sync_tool())

    with active_policy(_deny_policy()):
        with pytest.raises(ToolDeniedError):
            sync_tool.func(text="hello")


def test_sync_tool_denied_when_policy_requires_approval() -> None:
    """Treat approval_required as denied (until an approval handler intervenes)."""
    sync_tool = wrap_tool(_make_sync_tool())

    with active_policy(_approval_required_policy()):
        with pytest.raises(ToolDeniedError):
            sync_tool.func(text="hello")


def test_sync_tool_runs_when_policy_allows() -> None:
    """Forward to the original sync function when the tool is allowed."""
    sync_tool = wrap_tool(_make_sync_tool())

    with active_policy(_allow_policy()):
        result = sync_tool.func(text="hello")

    assert result == "echo:hello"


def test_sync_tool_invoke_returns_denial_message_via_handle_tool_error() -> None:
    """Surface the denial as tool output rather than aborting the graph."""
    sync_tool = wrap_tool(_make_sync_tool())

    result = sync_tool.invoke({"text": "hello"})

    assert isinstance(result, str)
    assert "denied by the agent policy" in result
    assert "echo" in result


@pytest.mark.asyncio
async def test_async_tool_denied_when_no_active_policy() -> None:
    """Raise ToolDeniedError when an async tool runs without a policy bound."""
    async_tool = wrap_tool(_make_async_tool())

    with pytest.raises(ToolDeniedError, match="no active Fortify policy"):
        await async_tool.coroutine(text="hello")


@pytest.mark.asyncio
async def test_async_tool_denied_when_policy_denies() -> None:
    """Raise ToolDeniedError when the active policy denies the async tool."""
    async_tool = wrap_tool(_make_async_tool())

    with active_policy(_deny_policy()):
        with pytest.raises(ToolDeniedError):
            await async_tool.coroutine(text="hello")


@pytest.mark.asyncio
async def test_async_tool_runs_when_policy_allows() -> None:
    """Forward to the original async function when the tool is allowed."""
    async_tool = wrap_tool(_make_async_tool())

    with active_policy(_allow_policy()):
        result = await async_tool.coroutine(text="hello")

    assert result == "async:hello"


@pytest.mark.asyncio
async def test_async_tool_ainvoke_returns_denial_message_via_handle_tool_error() -> (
    None
):
    """Surface async denials as tool output instead of bubbling the exception."""
    async_tool = wrap_tool(_make_async_tool())

    result = await async_tool.ainvoke({"text": "hello"})

    assert isinstance(result, str)
    assert "denied by the agent policy" in result


def test_wrap_tools_wraps_each_tool_and_returns_same_list() -> None:
    """Apply wrap_tool to every tool in-place and return the original list."""
    tools = [_make_sync_tool("echo_a"), _make_sync_tool("echo_b")]

    result = wrap_tools(tools)

    assert result is tools
    for t in tools:
        assert getattr(t, "_fortify_wrapped") is True
        assert t.handle_tool_error is True


def test_wrap_tools_isolates_policies_per_tool() -> None:
    """Each tool consults the active policy independently of the others."""
    tool_a = _make_sync_tool("tool_a")
    tool_b = _make_sync_tool("tool_b")
    wrap_tools([tool_a, tool_b])

    policy = AgentPolicy.model_validate(
        {
            "default_policy": {"mode": "deny"},
            "tools": {
                "tool_a": {"mode": "allow"},
                "tool_b": {"mode": "deny"},
            },
        }
    )

    with active_policy(policy):
        assert tool_a.func(text="x") == "echo:x"
        with pytest.raises(ToolDeniedError):
            tool_b.func(text="x")
