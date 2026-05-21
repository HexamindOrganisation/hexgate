"""Tests for the OpenAI Agents adapter agent wrapping helpers."""

from __future__ import annotations

from typing import Any

import pytest
from agents import Agent, FunctionTool

from fortify.adapters.openai.wrapper import build_policy_set, wrap_openai_agent
from fortify.runtime import User
from fortify.security import PolicySet
from fortify.security.policy_set import DEFAULT_ROLE_NAME


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


def test_build_policy_set_returns_allow_for_each_tool() -> None:
    """The placeholder policy builder allows every tool name it receives."""
    policy_set = build_policy_set(
        api_key="k",
        agent_name="my-agent",
        tool_names=["echo", "shout"],
    )

    assert isinstance(policy_set, PolicySet)
    default_policy = policy_set.policy_for(None)
    assert default_policy.tools["echo"].mode == "allow"
    assert default_policy.tools["shout"].mode == "allow"


def test_build_policy_set_with_no_tools_returns_empty_tools_dict() -> None:
    """No tool names → no per-tool entries; default policy still applies."""
    policy_set = build_policy_set(api_key="k", agent_name="my-agent", tool_names=[])

    assert policy_set.policy_for(None).tools == {}


def test_wrap_openai_agent_returns_a_new_agent_with_wrapped_tools() -> None:
    """wrap_openai_agent returns a clone whose tools are policy-gated copies."""
    original = _make_agent()
    original_tools = list(original.tools)

    wrapped = wrap_openai_agent(original, api_key="k")

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

    wrap_openai_agent(original, api_key="k")

    assert list(original.tools) == original_tools
    for tool, invoke in zip(original.tools, original_invokes):
        assert tool.on_invoke_tool is invoke


@pytest.mark.asyncio
async def test_wrap_openai_agent_installs_policy_gates_on_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cloned agent's tools call through the enforcer built from build_policy_set."""
    from fortify.security import AgentPolicy

    captured: dict[str, Any] = {}

    def fake_build(
        api_key: str,
        agent_name: str,
        tool_names: list[str],
    ) -> PolicySet:
        captured.update(
            {
                "api_key": api_key,
                "agent_name": agent_name,
                "tool_names": list(tool_names),
            }
        )
        return PolicySet(
            {
                DEFAULT_ROLE_NAME: AgentPolicy.model_validate(
                    {
                        "default_policy": {"mode": "deny"},
                        "tools": {name: {"mode": "deny"} for name in tool_names},
                    }
                )
            }
        )

    monkeypatch.setattr("fortify.adapters.openai.wrapper.build_policy_set", fake_build)

    original = _make_agent()

    wrapped = wrap_openai_agent(original, api_key="api-123")

    assert captured["api_key"] == "api-123"
    assert captured["agent_name"] == "my-agent"
    assert captured["tool_names"] == ["echo", "shout"]

    # Role resolution happens inside the enforcer at call time, so we need
    # an active User scope to exercise the gate end-to-end.
    [echo_tool, _] = wrapped.tools
    async with User(user_id="u-1"):
        result = await echo_tool.on_invoke_tool(None, '{"text": "hi"}')
    assert isinstance(result, str)
    assert "policy_denied" in result


def test_wrap_openai_agent_with_no_tools_returns_clone_with_empty_tools() -> None:
    """An agent with no tools wraps cleanly to a clone with no tools."""
    original = _make_agent(with_tools=False)

    wrapped = wrap_openai_agent(original, api_key="k")

    assert wrapped is not original
    assert list(wrapped.tools) == []
