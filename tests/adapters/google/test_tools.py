"""Tests for the Google ADK adapter policy gate on tools."""

from __future__ import annotations

from typing import Any

import pytest
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool

from fortify.adapters.google.tools import (
    _denial_message,
    _normalize,
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


def _make_callable(name: str = "echo") -> Any:
    """Create a plain callable that ADK will wrap as a FunctionTool."""

    def echo(text: str) -> str:
        """Echo the input back."""
        return f"echo:{text}"

    echo.__name__ = name
    return echo


def _make_function_tool(name: str = "echo") -> FunctionTool:
    """Create an ADK FunctionTool wrapping a sync echo function."""
    return FunctionTool(func=_make_callable(name))


def test_denial_message_includes_tool_name() -> None:
    """The denial message identifies the blocked tool by name."""
    msg = _denial_message("read_file")

    assert "read_file" in msg
    assert "denied" in msg
    assert "not executed" in msg


def test_normalize_passes_base_tool_through() -> None:
    """A BaseTool input is returned unchanged, not re-wrapped."""
    tool = _make_function_tool()

    assert _normalize(tool) is tool


def test_normalize_wraps_callable_into_function_tool() -> None:
    """A plain callable is wrapped into a FunctionTool the gate can attach to."""
    fn = _make_callable("custom_name")

    normalized = _normalize(fn)

    assert isinstance(normalized, FunctionTool)
    assert normalized.name == "custom_name"


def test_normalize_rejects_non_callable_non_tool() -> None:
    """Refuse to normalize anything that is neither a BaseTool nor callable."""
    with pytest.raises(TypeError, match="BaseTool or callable"):
        _normalize(42)  # type: ignore[arg-type]


def test_wrap_tool_returns_a_distinct_copy() -> None:
    """wrap_tool returns a new BaseTool instance, leaving the original alone."""
    original = _make_function_tool()

    wrapped = wrap_tool(original, _allow_policy())

    assert wrapped is not original
    assert wrapped.run_async != original.run_async


def test_wrap_tool_preserves_metadata() -> None:
    """The wrapped tool keeps the original tool's name."""
    original = _make_function_tool("custom_tool")

    wrapped = wrap_tool(original, _allow_policy("custom_tool"))

    assert wrapped.name == "custom_tool"


def test_wrap_tool_accepts_plain_callable() -> None:
    """Wrapping a callable normalizes it into a FunctionTool with a gate."""
    fn = _make_callable("custom_name")

    wrapped = wrap_tool(fn, _allow_policy("custom_name"))

    assert isinstance(wrapped, BaseTool)
    assert wrapped.name == "custom_name"


@pytest.mark.asyncio
async def test_wrapped_tool_returns_denial_message_when_policy_denies() -> None:
    """A deny-mode policy short-circuits to the denial string instead of raising."""
    wrapped = wrap_tool(_make_function_tool(), _deny_policy())

    result = await wrapped.run_async(args={"text": "hi"}, tool_context=None)

    assert isinstance(result, str)
    assert "denied" in result
    assert "echo" in result


@pytest.mark.asyncio
async def test_wrapped_tool_returns_denial_message_when_policy_requires_approval() -> (
    None
):
    """approval_required is treated as denied — the underlying tool never runs."""
    wrapped = wrap_tool(_make_function_tool(), _approval_required_policy())

    result = await wrapped.run_async(args={"text": "hi"}, tool_context=None)

    assert "denied" in result


@pytest.mark.asyncio
async def test_wrapped_tool_invokes_original_when_policy_allows() -> None:
    """An allow-mode policy forwards to the original run_async."""
    wrapped = wrap_tool(_make_function_tool(), _allow_policy())

    result = await wrapped.run_async(args={"text": "hi"}, tool_context=None)

    assert result == "echo:hi"


@pytest.mark.asyncio
async def test_original_tool_is_not_mutated_by_wrap_tool() -> None:
    """The original tool can still be invoked directly with its original behavior."""
    original = _make_function_tool()

    wrap_tool(original, _deny_policy())

    result = await original.run_async(args={"text": "hi"}, tool_context=None)
    assert result == "echo:hi"


def test_wrap_tools_returns_distinct_list_of_copies() -> None:
    """wrap_tools returns a fresh list of fresh wrapped copies."""
    originals = [_make_function_tool("a"), _make_function_tool("b")]
    policy = AgentPolicy.model_validate(
        {
            "default_policy": {"mode": "deny"},
            "tools": {"a": {"mode": "allow"}, "b": {"mode": "allow"}},
        }
    )

    wrapped = wrap_tools(originals, policy)

    assert wrapped is not originals
    assert len(wrapped) == 2
    for original_tool, wrapped_tool in zip(originals, wrapped):
        assert wrapped_tool is not original_tool
        assert wrapped_tool.name == original_tool.name


@pytest.mark.asyncio
async def test_wrap_tools_isolates_policy_decisions_per_tool() -> None:
    """Each wrapped tool follows its own per-name decision under the same policy."""
    originals = [_make_function_tool("tool_a"), _make_function_tool("tool_b")]
    policy = AgentPolicy.model_validate(
        {
            "default_policy": {"mode": "deny"},
            "tools": {
                "tool_a": {"mode": "allow"},
                "tool_b": {"mode": "deny"},
            },
        }
    )
    [tool_a, tool_b] = wrap_tools(originals, policy)

    allowed = await tool_a.run_async(args={"text": "x"}, tool_context=None)
    denied = await tool_b.run_async(args={"text": "x"}, tool_context=None)

    assert allowed == "echo:x"
    assert "denied" in denied


@pytest.mark.asyncio
async def test_wrap_tools_accepts_mixed_callables_and_base_tools() -> None:
    """A mix of callables and BaseTools is normalized and gated uniformly."""
    fn = _make_callable("plain_fn")
    tool = _make_function_tool("tool_obj")
    policy = AgentPolicy.model_validate(
        {
            "default_policy": {"mode": "deny"},
            "tools": {
                "plain_fn": {"mode": "allow"},
                "tool_obj": {"mode": "deny"},
            },
        }
    )

    wrapped = wrap_tools([fn, tool], policy)

    [plain, gated] = wrapped
    assert plain.name == "plain_fn"
    assert gated.name == "tool_obj"
    allowed = await plain.run_async(args={"text": "x"}, tool_context=None)
    denied = await gated.run_async(args={"text": "x"}, tool_context=None)
    assert allowed == "echo:x"
    assert "denied" in denied
