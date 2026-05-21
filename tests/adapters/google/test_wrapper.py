"""Tests for the Google ADK adapter agent wrapping helpers."""

from __future__ import annotations

from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.tools.function_tool import FunctionTool

from fortify.adapters.google.wrapper import build_policy_set, wrap_google_agent
from fortify.runtime import User
from fortify.security import PolicySet


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


# ---------------------------------------------------------------------------
# build_policy_set
# ---------------------------------------------------------------------------


def test_build_policy_set_allows_each_tool_under_default_role() -> None:
    """The placeholder policy builder allows every tool name it receives."""
    policy_set = build_policy_set(
        api_key="k", agent_name="my_agent", tool_names=["echo", "shout"]
    )

    assert isinstance(policy_set, PolicySet)
    default_policy = policy_set.policy_for(None)
    assert default_policy.tools["echo"].mode == "allow"
    assert default_policy.tools["shout"].mode == "allow"


def test_build_policy_set_with_no_tools_returns_empty_tools_map() -> None:
    policy_set = build_policy_set("k", "my_agent", [])

    assert policy_set.policy_for(None).tools == {}


# ---------------------------------------------------------------------------
# wrap_google_agent — clone + non-mutation
# ---------------------------------------------------------------------------


def test_wrap_google_agent_returns_a_new_agent_with_wrapped_tools() -> None:
    """Returns a clone whose tools are policy-gated copies."""
    original = _make_agent()

    wrapped = wrap_google_agent(original, api_key="k")

    assert wrapped is not original
    assert wrapped.name == original.name
    assert len(wrapped.tools) == len(original.tools) == 2


def test_wrap_google_agent_does_not_mutate_original_agent() -> None:
    """The original agent's tool list is left untouched after wrapping."""
    original = _make_agent()
    original_tools = list(original.tools)

    wrap_google_agent(original, api_key="k")

    assert list(original.tools) == original_tools


def test_wrap_google_agent_with_no_tools_returns_clone_with_empty_tools() -> None:
    original = _make_agent(with_tools=False)

    wrapped = wrap_google_agent(original, api_key="k")

    assert wrapped is not original
    assert list(wrapped.tools) == []


# ---------------------------------------------------------------------------
# wrap_google_agent — enforcer wiring (verified end-to-end via a stubbed policy)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrap_google_agent_installs_enforcer_with_built_policy_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cloned agent's tools enforce the PolicySet returned by build_policy_set."""
    from fortify.security import AgentPolicy
    from fortify.security.policy_set import DEFAULT_ROLE_NAME

    captured: dict[str, Any] = {}

    def fake_build(api_key: str, agent_name: str, tool_names: list[str]) -> PolicySet:
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
                        "tools": {n: {"mode": "deny"} for n in tool_names},
                    }
                )
            }
        )

    monkeypatch.setattr(
        "fortify.adapters.google.wrapper.build_policy_set", fake_build
    )

    original = _make_agent()
    wrapped = wrap_google_agent(original, api_key="api-123")

    assert captured["api_key"] == "api-123"
    assert captured["agent_name"] == "my_agent"
    assert sorted(captured["tool_names"]) == ["echo", "shout"]

    # Role resolution at call time → no User scope → default → deny.
    [echo_tool, shout_tool] = wrapped.tools
    echo_result = await echo_tool.run_async(args={"text": "hi"}, tool_context=None)
    shout_result = await shout_tool.run_async(args={"text": "hi"}, tool_context=None)
    assert "policy_denied" in echo_result
    assert "policy_denied" in shout_result


@pytest.mark.asyncio
async def test_wrap_google_agent_resolves_role_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role-aware PolicySet routes per-call via the active User's role."""
    from fortify.security import AgentPolicy
    from fortify.security.policy_set import DEFAULT_ROLE_NAME

    monkeypatch.setattr(
        "fortify.adapters.google.wrapper.build_policy_set",
        lambda api_key, agent_name, tool_names: PolicySet(
            {
                DEFAULT_ROLE_NAME: AgentPolicy.model_validate(
                    {"default_policy": {"mode": "deny"}}
                ),
                "support": AgentPolicy.model_validate(
                    {
                        "default_policy": {"mode": "deny"},
                        "tools": {n: {"mode": "allow"} for n in tool_names},
                    }
                ),
            }
        ),
    )

    wrapped = wrap_google_agent(_make_agent(), api_key="k")
    [echo_tool, _] = wrapped.tools

    # No User → deny.
    denied = await echo_tool.run_async(args={"text": "hi"}, tool_context=None)
    assert "policy_denied" in denied

    # support → allow.
    async with User(user_id="u-1", role="support"):
        allowed = await echo_tool.run_async(args={"text": "hi"}, tool_context=None)
    assert allowed == "echo:hi"


def test_wrap_google_agent_passes_tool_names_for_callable_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain callables expose their __name__ as the policy tool name."""
    captured: dict[str, list[str]] = {}

    def fake_build(api_key: str, agent_name: str, tool_names: list[str]) -> PolicySet:
        captured["tool_names"] = list(tool_names)
        return build_policy_set(api_key, agent_name, tool_names)

    monkeypatch.setattr(
        "fortify.adapters.google.wrapper.build_policy_set", fake_build
    )

    wrap_google_agent(_make_agent(), api_key="k")

    assert sorted(captured["tool_names"]) == ["echo", "shout"]
