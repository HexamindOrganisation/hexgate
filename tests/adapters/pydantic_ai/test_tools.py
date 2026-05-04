"""Tests for the pydantic_ai adapter policy gate on tools."""

from __future__ import annotations

import pytest
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import Tool

from fortify.adapters.pydantic_ai.tools import (
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


def _make_sync_tool(name: str = "echo") -> Tool:
    """Create a pydantic_ai Tool with a sync function."""

    def echo(text: str) -> str:
        """Echo the input back."""
        return f"echo:{text}"

    return Tool(echo, name=name)


def _make_async_tool(name: str = "echo") -> Tool:
    """Create a pydantic_ai Tool with an async function."""

    async def echo(text: str) -> str:
        """Echo the input back asynchronously."""
        return f"async:{text}"

    return Tool(echo, name=name)


def test_tool_denied_error_is_a_model_retry() -> None:
    """ToolDeniedError must subclass ModelRetry so pydantic_ai surfaces it as tool output."""
    err = ToolDeniedError("read_file", "no active Fortify policy")

    assert isinstance(err, ModelRetry)
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
    from fortify.adapters.pydantic_ai import tools as pa_tools

    assert pa_tools._active_policy.get() is None

    policy = _allow_policy()
    with active_policy(policy):
        assert pa_tools._active_policy.get() is policy

    assert pa_tools._active_policy.get() is None


def test_active_policy_resets_after_exception() -> None:
    """Reset the contextvar even when the body raises."""
    from fortify.adapters.pydantic_ai import tools as pa_tools

    policy = _allow_policy()
    with pytest.raises(RuntimeError, match="boom"):
        with active_policy(policy):
            assert pa_tools._active_policy.get() is policy
            raise RuntimeError("boom")

    assert pa_tools._active_policy.get() is None


def test_wrap_tool_returns_a_distinct_copy() -> None:
    """wrap_tool returns a new Tool whose function_schema is a fresh copy."""
    original = _make_sync_tool()

    wrapped = wrap_tool(original)

    assert wrapped is not original
    assert wrapped.function_schema is not original.function_schema


def test_wrap_tool_preserves_tool_name() -> None:
    """The wrapped tool keeps the original name so the model can address it."""
    original = _make_sync_tool("custom_name")

    wrapped = wrap_tool(original)

    assert wrapped.name == "custom_name"


@pytest.mark.asyncio
async def test_wrapped_sync_tool_denied_when_no_active_policy() -> None:
    """Calling the gated tool without a policy bound surfaces a ToolDeniedError."""
    wrapped = wrap_tool(_make_sync_tool())

    with pytest.raises(ToolDeniedError, match="no active Fortify policy"):
        await wrapped.function_schema.call({"text": "hi"}, None)


@pytest.mark.asyncio
async def test_wrapped_sync_tool_denied_when_policy_denies() -> None:
    """A deny-mode policy raises ToolDeniedError without invoking the function."""
    wrapped = wrap_tool(_make_sync_tool())

    with active_policy(_deny_policy()):
        with pytest.raises(ToolDeniedError):
            await wrapped.function_schema.call({"text": "hi"}, None)


@pytest.mark.asyncio
async def test_wrapped_sync_tool_denied_when_policy_requires_approval() -> None:
    """approval_required is treated as denied until an approval handler intervenes."""
    wrapped = wrap_tool(_make_sync_tool())

    with active_policy(_approval_required_policy()):
        with pytest.raises(ToolDeniedError):
            await wrapped.function_schema.call({"text": "hi"}, None)


@pytest.mark.asyncio
async def test_wrapped_sync_tool_runs_when_policy_allows() -> None:
    """Forward to the original sync function when the tool is allowed."""
    wrapped = wrap_tool(_make_sync_tool())

    with active_policy(_allow_policy()):
        result = await wrapped.function_schema.call({"text": "hi"}, None)

    assert result == "echo:hi"


@pytest.mark.asyncio
async def test_wrapped_async_tool_runs_when_policy_allows() -> None:
    """Forward to the original async function when the tool is allowed."""
    wrapped = wrap_tool(_make_async_tool())

    with active_policy(_allow_policy()):
        result = await wrapped.function_schema.call({"text": "hi"}, None)

    assert result == "async:hi"


@pytest.mark.asyncio
async def test_wrapped_async_tool_denied_when_no_active_policy() -> None:
    """Async tools also raise ToolDeniedError without a policy bound."""
    wrapped = wrap_tool(_make_async_tool())

    with pytest.raises(ToolDeniedError, match="no active Fortify policy"):
        await wrapped.function_schema.call({"text": "hi"}, None)


@pytest.mark.asyncio
async def test_original_tool_is_not_mutated_by_wrap_tool() -> None:
    """wrap_tool must not install gates on the input Tool — only on the copy."""
    original = _make_sync_tool()

    wrap_tool(original)

    result = await original.function_schema.call({"text": "hi"}, None)
    assert result == "echo:hi"


def test_wrap_tools_returns_distinct_list_of_copies() -> None:
    """wrap_tools returns a fresh list of wrapped copies, leaving originals alone."""
    originals = [_make_sync_tool("a"), _make_sync_tool("b")]

    wrapped = wrap_tools(originals)

    assert wrapped is not originals
    assert len(wrapped) == 2
    for original_tool, wrapped_tool in zip(originals, wrapped):
        assert wrapped_tool is not original_tool
        assert wrapped_tool.name == original_tool.name


@pytest.mark.asyncio
async def test_wrap_tools_isolates_policies_per_tool() -> None:
    """Each wrapped tool consults the active policy independently."""
    originals = [_make_sync_tool("tool_a"), _make_sync_tool("tool_b")]
    [tool_a, tool_b] = wrap_tools(originals)

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
        assert await tool_a.function_schema.call({"text": "x"}, None) == "echo:x"
        with pytest.raises(ToolDeniedError):
            await tool_b.function_schema.call({"text": "x"}, None)
