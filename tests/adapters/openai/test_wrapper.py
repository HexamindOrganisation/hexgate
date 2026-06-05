"""Tests for the OpenAI Agents adapter agent wrapping helpers (phase 6).

The allow-all ``build_policy_set`` placeholder is gone. The wrapper is
now mechanics-only — ``wrap_openai_agent(agent, enforcer=...)`` clones
with gated tools — while policy resolution lives in
:func:`_resolve_binding` (platform pull, register-on-404) and the
lifecycle (binding cache + per-run refresh) lives in the runner.
"""

from __future__ import annotations

from typing import Any

import pytest
from agents import Agent, FunctionTool

from fortify.adapters.openai import wrapper as wrapper_mod
from fortify.adapters.openai.wrapper import wrap_openai_agent
from fortify.cloud.client import FortifyError
from fortify.runtime import User
from fortify.security import AgentPolicy, BaseToolPolicy, PolicyBinding, PolicySet
from fortify.security.enforcer import PolicyEnforcer
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


def _allow_all_enforcer(tool_names: list[str]) -> PolicyEnforcer:
    engine = PolicySet(
        {
            DEFAULT_ROLE_NAME: AgentPolicy(
                tools={n: BaseToolPolicy(mode="allow") for n in tool_names}
            )
        }
    )
    return PolicyEnforcer(engine, agent_name="my-agent")


def _deny_all_enforcer() -> PolicyEnforcer:
    engine = PolicySet(
        {
            DEFAULT_ROLE_NAME: AgentPolicy.model_validate(
                {"default_policy": {"mode": "deny"}}
            )
        }
    )
    return PolicyEnforcer(engine, agent_name="my-agent")


# ---------------------------------------------------------------------------
# wrap_openai_agent — clone + non-mutation (mechanics only, enforcer passed in)
# ---------------------------------------------------------------------------


def test_wrap_openai_agent_returns_a_new_agent_with_wrapped_tools() -> None:
    """wrap_openai_agent returns a clone whose tools are policy-gated copies."""
    original = _make_agent()
    original_tools = list(original.tools)

    wrapped = wrap_openai_agent(
        original, enforcer=_allow_all_enforcer(["echo", "shout"])
    )

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

    wrap_openai_agent(original, enforcer=_allow_all_enforcer(["echo", "shout"]))

    assert list(original.tools) == original_tools
    for tool, invoke in zip(original.tools, original_invokes):
        assert tool.on_invoke_tool is invoke


def test_wrap_openai_agent_with_no_tools_returns_clone_with_empty_tools() -> None:
    """An agent with no tools wraps cleanly to a clone with no tools."""
    original = _make_agent(with_tools=False)

    wrapped = wrap_openai_agent(original, enforcer=_allow_all_enforcer([]))

    assert wrapped is not original
    assert list(wrapped.tools) == []


@pytest.mark.asyncio
async def test_wrap_openai_agent_gates_tools_with_the_given_enforcer() -> None:
    """The cloned agent's tools call through the supplied enforcer."""
    wrapped = wrap_openai_agent(_make_agent(), enforcer=_deny_all_enforcer())

    [echo_tool, _] = wrapped.tools
    async with User(user_id="u-1"):
        result = await echo_tool.on_invoke_tool(None, '{"text": "hi"}')
    assert isinstance(result, str)
    assert "policy_denied" in result


@pytest.mark.asyncio
async def test_refresh_swap_reaches_every_clone() -> None:
    """Rebinding enforcer.policy (what refresh does) flips decisions in ALL
    clones produced from the shared enforcer — the per-call rewrap is safe."""
    enforcer = _deny_all_enforcer()
    first_clone = wrap_openai_agent(_make_agent(), enforcer=enforcer)
    second_clone = wrap_openai_agent(_make_agent(), enforcer=enforcer)

    async with User(user_id="u-1"):
        denied = await first_clone.tools[0].on_invoke_tool(None, '{"text": "x"}')
        assert "policy_denied" in denied

        enforcer.policy = PolicySet(
            {
                DEFAULT_ROLE_NAME: AgentPolicy(
                    tools={
                        "echo": BaseToolPolicy(mode="allow"),
                        "shout": BaseToolPolicy(mode="allow"),
                    }
                )
            }
        )  # the refresh swap

        for clone in (first_clone, second_clone):
            allowed = await clone.tools[0].on_invoke_tool(None, '{"text": "x"}')
            assert allowed == 'invoked:{"text": "x"}'


# ---------------------------------------------------------------------------
# _resolve_binding — 404 → register → retry; everything else loud
# ---------------------------------------------------------------------------


def test_404_registers_openai_agent_then_resolves_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fortify.cli.register as register_pkg

    calls: list[str] = []
    registered: list[Any] = []
    stub = PolicyBinding(_allow_all_enforcer(["echo"]))

    def fake_resolve(name: str, *, api_key: str | None = None, client: Any = None):
        calls.append(name)
        if len(calls) == 1:
            raise FortifyError("Fortify API error 404 calling …", status=404)
        return stub

    monkeypatch.setattr(
        wrapper_mod.PolicyBinding, "resolve", staticmethod(fake_resolve)
    )
    monkeypatch.setattr(
        register_pkg, "register_agent", lambda agent: registered.append(agent)
    )

    agent = _make_agent()
    binding = wrapper_mod._resolve_binding(agent, "my-agent", "k")

    assert binding is stub
    assert calls == ["my-agent", "my-agent"]
    assert registered == [agent]  # the introspectable Agent object itself


def test_non_404_failure_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running asked for governance — a platform error never yields a
    silently allow-all agent."""

    def fake_resolve(name: str, *, api_key: str | None = None, client: Any = None):
        raise FortifyError("Fortify API error 500 calling …", status=500)

    monkeypatch.setattr(
        wrapper_mod.PolicyBinding, "resolve", staticmethod(fake_resolve)
    )

    with pytest.raises(FortifyError, match="500"):
        wrapper_mod._resolve_binding(_make_agent(), "my-agent", "k")
