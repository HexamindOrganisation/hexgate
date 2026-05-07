"""Tests for the Google ADK adapter agent wrapping helpers."""

from __future__ import annotations

from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.tools.function_tool import FunctionTool

from fortify.adapters.google.wrapper import build_agent_policy, wrap_google_agent
from fortify.security import AgentPolicy
from fortify.user_context import UserContext


def _user_context() -> UserContext:
    """Build a minimal UserContext for wrapper tests."""
    return UserContext(user_id="u-1", session_id="s-1", user_role="developer")


def _make_callable(name: str = "echo") -> Any:
    """Build a plain callable echo function with a settable __name__."""

    def echo(text: str) -> str:
        """Echo the input back."""
        return f"echo:{text}"

    echo.__name__ = name
    return echo


def _make_agent(name: str = "my_agent", *, with_tools: bool = True) -> LlmAgent:
    """Build a minimal LlmAgent fixture for wrapper tests."""
    tools: list[Any] = (
        [FunctionTool(func=_make_callable("echo")), _make_callable("shout")]
        if with_tools
        else []
    )
    return LlmAgent(name=name, model="gemini-2.0-flash", tools=tools)


def test_build_agent_policy_returns_allow_for_each_tool() -> None:
    """The placeholder policy builder allows every tool name it receives."""
    policy = build_agent_policy(
        api_key="k",
        context=_user_context(),
        agent_name="my_agent",
        tool_names=["echo", "shout"],
    )

    assert isinstance(policy, AgentPolicy)
    assert policy.tools["echo"].mode == "allow"
    assert policy.tools["shout"].mode == "allow"


def test_build_agent_policy_with_no_tools_returns_empty_tools_dict() -> None:
    """No tool names → no per-tool entries; default policy still applies."""
    policy = build_agent_policy(
        api_key="k", context=_user_context(), agent_name="my_agent", tool_names=[]
    )

    assert policy.tools == {}


def test_wrap_google_agent_returns_a_new_agent_with_wrapped_tools() -> None:
    """wrap_google_agent returns a clone whose tools are policy-gated copies."""
    original = _make_agent()

    wrapped = wrap_google_agent(original, _user_context(), api_key="k")

    assert wrapped is not original
    assert wrapped.name == original.name
    assert len(wrapped.tools) == len(original.tools) == 2


def test_wrap_google_agent_does_not_mutate_original_agent() -> None:
    """The original agent's tool list is left untouched after wrapping."""
    original = _make_agent()
    original_tools = list(original.tools)

    wrap_google_agent(original, _user_context(), api_key="k")

    assert list(original.tools) == original_tools


@pytest.mark.asyncio
async def test_wrap_google_agent_installs_policy_gates_built_from_user_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cloned agent's tools enforce the policy returned by build_agent_policy."""
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
        "fortify.adapters.google.wrapper.build_agent_policy", fake_build
    )

    original = _make_agent()
    ctx = _user_context()

    wrapped = wrap_google_agent(original, ctx, api_key="api-123")

    assert captured["api_key"] == "api-123"
    assert captured["context"] is ctx
    assert captured["agent_name"] == "my_agent"
    assert sorted(captured["tool_names"]) == ["echo", "shout"]

    [echo_tool, shout_tool] = wrapped.tools
    echo_result = await echo_tool.run_async(args={"text": "hi"}, tool_context=None)
    shout_result = await shout_tool.run_async(args={"text": "hi"}, tool_context=None)
    assert "denied" in echo_result
    assert "denied" in shout_result


def test_wrap_google_agent_uses_function_name_for_callable_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain callables expose their __name__ as the policy tool name."""
    captured: dict[str, list[str]] = {}

    def fake_build(
        api_key: str,
        context: UserContext,
        agent_name: str,
        tool_names: list[str],
    ) -> AgentPolicy:
        captured["tool_names"] = list(tool_names)
        return AgentPolicy.model_validate({"default_policy": {"mode": "allow"}})

    monkeypatch.setattr(
        "fortify.adapters.google.wrapper.build_agent_policy", fake_build
    )

    wrap_google_agent(_make_agent(), _user_context(), api_key="k")

    assert sorted(captured["tool_names"]) == ["echo", "shout"]


def test_wrap_google_agent_with_no_tools_returns_clone_with_empty_tools() -> None:
    """An agent with no tools wraps cleanly to a clone with no tools."""
    original = _make_agent(with_tools=False)

    wrapped = wrap_google_agent(original, _user_context(), api_key="k")

    assert wrapped is not original
    assert list(wrapped.tools) == []
