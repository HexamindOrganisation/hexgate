"""Tests for the OpenAI Agents adapter agent wrapping helpers."""

from __future__ import annotations

from typing import Any

import pytest
from agents import Agent, FunctionTool

from fortify.adapters.openai.wrapper import build_agent_policy, wrap_openai_agent
from fortify.security import AgentPolicy
from fortify.user_context import UserContext


def _user_context() -> UserContext:
    """Build a minimal UserContext for wrapper tests."""
    return UserContext(user_id="u-1", session_id="s-1", user_role="developer")


def _make_tool(name: str = "echo") -> FunctionTool:
    """Build a minimal FunctionTool that records every invocation it receives."""

    async def on_invoke(_ctx: Any, raw_args: str) -> str:
        return f"invoked:{raw_args}"

    return FunctionTool(
        name=name,
        description=f"{name} tool",
        params_json_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        on_invoke_tool=on_invoke,
    )


def _make_agent(name: str = "my-agent", *, with_tools: bool = True) -> Agent:
    """Build an agents.Agent fixture for wrapper tests."""
    tools = [_make_tool("echo"), _make_tool("shout")] if with_tools else []
    return Agent(name=name, tools=tools)


def test_build_agent_policy_returns_allow_for_each_tool() -> None:
    """The placeholder policy builder allows every tool name it receives."""
    policy = build_agent_policy(
        api_key="k",
        context=_user_context(),
        agent_name="my-agent",
        tool_names=["echo", "shout"],
    )

    assert isinstance(policy, AgentPolicy)
    assert policy.tools["echo"].mode == "allow"
    assert policy.tools["shout"].mode == "allow"


def test_build_agent_policy_with_no_tools_returns_empty_tools_dict() -> None:
    """No tool names → no per-tool entries; default policy still applies."""
    policy = build_agent_policy(
        api_key="k", context=_user_context(), agent_name="my-agent", tool_names=[]
    )

    assert policy.tools == {}


def test_wrap_openai_agent_returns_a_new_agent_with_wrapped_tools() -> None:
    """wrap_openai_agent returns a clone whose tools are policy-gated copies."""
    original = _make_agent()
    original_tools = list(original.tools)

    wrapped = wrap_openai_agent(original, _user_context(), api_key="k")

    assert wrapped is not original
    assert wrapped.name == original.name
    assert [t.name for t in wrapped.tools] == [t.name for t in original_tools]
    for original_tool, wrapped_tool in zip(original_tools, wrapped.tools):
        assert wrapped_tool is not original_tool


def test_wrap_openai_agent_does_not_mutate_original_agent() -> None:
    """The original agent's tool list and tool callables are left untouched."""
    original = _make_agent()
    original_tools = list(original.tools)
    original_invokes = [t.on_invoke_tool for t in original_tools]

    wrap_openai_agent(original, _user_context(), api_key="k")

    assert list(original.tools) == original_tools
    for tool, invoke in zip(original.tools, original_invokes):
        assert tool.on_invoke_tool is invoke


@pytest.mark.asyncio
async def test_wrap_openai_agent_installs_policy_gates_on_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cloned agent's tools call through the gate built from build_agent_policy."""
    captured: dict[str, Any] = {}

    def fake_build(
        api_key: str,
        context: UserContext,
        agent_name: str,
        tool_names: list[str],
    ) -> AgentPolicy:
        captured.update(
            {
                "api_key": api_key,
                "context": context,
                "agent_name": agent_name,
                "tool_names": list(tool_names),
            }
        )
        return AgentPolicy.model_validate(
            {
                "default_policy": {"mode": "deny"},
                "tools": {name: {"mode": "deny"} for name in tool_names},
            }
        )

    monkeypatch.setattr(
        "fortify.adapters.openai.wrapper.build_agent_policy", fake_build
    )

    original = _make_agent()
    ctx = _user_context()

    wrapped = wrap_openai_agent(original, ctx, api_key="api-123")

    assert captured["api_key"] == "api-123"
    assert captured["context"] is ctx
    assert captured["agent_name"] == "my-agent"
    assert captured["tool_names"] == ["echo", "shout"]

    [echo_tool, _] = wrapped.tools
    result = await echo_tool.on_invoke_tool(None, '{"text": "hi"}')
    assert isinstance(result, str)
    assert "denied" in result


def test_wrap_openai_agent_with_no_tools_returns_clone_with_empty_tools() -> None:
    """An agent with no tools wraps cleanly to a clone with no tools."""
    original = _make_agent(with_tools=False)

    wrapped = wrap_openai_agent(original, _user_context(), api_key="k")

    assert wrapped is not original
    assert list(wrapped.tools) == []
