"""Tests for the pydantic_ai adapter policy gate on tools."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import Tool

from hexgate.adapters.pydantic_ai.tools import wrap_tool, wrap_tools
from hexgate.runtime import User
from hexgate.security import AgentPolicy, PolicySet
from hexgate.security.enforcer import PolicyEnforcer
from hexgate.security.policy_set import DEFAULT_ROLE_NAME


def _enforcer_for(spec: dict[str, Any]) -> PolicyEnforcer:
    return PolicyEnforcer(
        PolicySet({DEFAULT_ROLE_NAME: AgentPolicy.model_validate(spec)})
    )


def _allow_enforcer(tool_name: str = "echo") -> PolicyEnforcer:
    return _enforcer_for(
        {
            "default_policy": {"mode": "deny"},
            "tools": {tool_name: {"mode": "allow"}},
        }
    )


def _deny_enforcer() -> PolicyEnforcer:
    return _enforcer_for({"default_policy": {"mode": "deny"}})


def _approval_enforcer(tool_name: str = "echo") -> PolicyEnforcer:
    return _enforcer_for(
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


# ---------------------------------------------------------------------------
# wrap_tool — basic shape
# ---------------------------------------------------------------------------


def test_wrap_tool_returns_a_distinct_copy() -> None:
    """wrap_tool returns a new Tool whose function_schema is a fresh copy."""
    original = _make_sync_tool()

    wrapped = wrap_tool(original, _allow_enforcer())

    assert wrapped is not original
    assert wrapped.function_schema is not original.function_schema


def test_wrap_tool_preserves_tool_name() -> None:
    """The wrapped tool keeps the original name so the model can address it."""
    original = _make_sync_tool("custom_name")

    wrapped = wrap_tool(original, _allow_enforcer("custom_name"))

    assert wrapped.name == "custom_name"


# ---------------------------------------------------------------------------
# Gated call — sync tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_tool_allowed_runs_original() -> None:
    wrapped = wrap_tool(_make_sync_tool(), _allow_enforcer())

    result = await wrapped.function_schema.call({"text": "hi"}, None)

    assert result == "echo:hi"


@pytest.mark.asyncio
async def test_sync_tool_denied_raises_model_retry_with_marker() -> None:
    wrapped = wrap_tool(_make_sync_tool(), _deny_enforcer())

    with pytest.raises(ModelRetry, match="policy_denied"):
        await wrapped.function_schema.call({"text": "hi"}, None)


@pytest.mark.asyncio
async def test_sync_tool_needs_approval_raises_marker() -> None:
    """NEEDS_APPROVAL always raises ModelRetry with the approval_required marker."""
    wrapped = wrap_tool(_make_sync_tool(), _approval_enforcer())

    with pytest.raises(ModelRetry, match="approval_required"):
        await wrapped.function_schema.call({"text": "hi"}, None)


# ---------------------------------------------------------------------------
# Async tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_tool_allowed_runs_original() -> None:
    wrapped = wrap_tool(_make_async_tool(), _allow_enforcer())

    result = await wrapped.function_schema.call({"text": "hi"}, None)

    assert result == "async:hi"


@pytest.mark.asyncio
async def test_async_tool_denied_raises_model_retry() -> None:
    wrapped = wrap_tool(_make_async_tool(), _deny_enforcer())

    with pytest.raises(ModelRetry, match="policy_denied"):
        await wrapped.function_schema.call({"text": "hi"}, None)


# ---------------------------------------------------------------------------
# Role resolution via User contextvar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_role_selects_matching_policy() -> None:
    """The active User's role drives which AgentPolicy the enforcer applies."""
    policy_set = PolicySet(
        {
            DEFAULT_ROLE_NAME: AgentPolicy.model_validate(
                {"default_policy": {"mode": "deny"}}
            ),
            "support": AgentPolicy.model_validate(
                {
                    "default_policy": {"mode": "deny"},
                    "tools": {"echo": {"mode": "allow"}},
                }
            ),
        }
    )
    wrapped = wrap_tool(_make_sync_tool(), PolicyEnforcer(policy_set))

    # No User → default → deny.
    with pytest.raises(ModelRetry, match="policy_denied"):
        await wrapped.function_schema.call({"text": "hi"}, None)

    # support → allow.
    async with User(user_id="u-1", role="support"):
        result = await wrapped.function_schema.call({"text": "hi"}, None)
    assert result == "echo:hi"


# ---------------------------------------------------------------------------
# Batch wrap_tools
# ---------------------------------------------------------------------------


def test_wrap_tools_returns_list_of_copies() -> None:
    originals = [_make_sync_tool("a"), _make_sync_tool("b")]
    enforcer = _enforcer_for(
        {
            "default_policy": {"mode": "deny"},
            "tools": {"a": {"mode": "allow"}, "b": {"mode": "allow"}},
        }
    )

    wrapped = wrap_tools(originals, enforcer)

    assert len(wrapped) == 2
    for original_tool, wrapped_tool in zip(originals, wrapped):
        assert wrapped_tool is not original_tool
        assert wrapped_tool.name == original_tool.name


@pytest.mark.asyncio
async def test_wrap_tools_isolates_decisions_per_tool() -> None:
    originals = [_make_sync_tool("tool_a"), _make_sync_tool("tool_b")]
    enforcer = _enforcer_for(
        {
            "default_policy": {"mode": "deny"},
            "tools": {
                "tool_a": {"mode": "allow"},
                "tool_b": {"mode": "deny"},
            },
        }
    )
    [tool_a, tool_b] = wrap_tools(originals, enforcer)

    allowed = await tool_a.function_schema.call({"text": "x"}, None)
    assert allowed == "echo:x"

    with pytest.raises(ModelRetry, match="policy_denied"):
        await tool_b.function_schema.call({"text": "x"}, None)
