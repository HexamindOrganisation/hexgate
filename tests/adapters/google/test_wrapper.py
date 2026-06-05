"""Tests for the Google ADK adapter agent wrapping helpers (phase 5).

The allow-all ``build_policy_set`` placeholder is gone: wrap-time policy
comes from :meth:`PolicyBinding.resolve` (platform / local override),
with register-on-404 from the in-code ADK definition. These tests stub
the resolve seam (``wrapper._resolve_binding``) so no platform is
needed.
"""

from __future__ import annotations

from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.tools.function_tool import FunctionTool

from fortify.adapters.google import wrapper as wrapper_mod
from fortify.adapters.google.wrapper import wrap_google_agent
from fortify.cloud.client import FortifyError
from fortify.runtime import User
from fortify.security import AgentPolicy, BaseToolPolicy, PolicyBinding, PolicySet
from fortify.security.enforcer import PolicyEnforcer
from fortify.security.policy_set import DEFAULT_ROLE_NAME


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


def _engine(spec: dict[str, Any]) -> PolicySet:
    return PolicySet({DEFAULT_ROLE_NAME: AgentPolicy.model_validate(spec)})


def _allow_all(tool_names: list[str]) -> PolicySet:
    return PolicySet(
        {
            DEFAULT_ROLE_NAME: AgentPolicy(
                tools={n: BaseToolPolicy(mode="allow") for n in tool_names}
            )
        }
    )


def _stub_binding(engine: PolicySet) -> PolicyBinding:
    return PolicyBinding(PolicyEnforcer(engine, agent_name="my_agent"))


@pytest.fixture()
def resolved(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the resolve seam with an allow-all engine; capture the call."""
    captured: dict[str, Any] = {}

    def fake_resolve(agent: Any, name: str, key: str) -> PolicyBinding:
        captured.update(agent=agent, name=name, key=key)
        tool_names = [
            getattr(t, "name", getattr(t, "__name__", "tool"))
            for t in (getattr(agent, "tools", []) or [])
        ]
        return _stub_binding(_allow_all(tool_names))

    monkeypatch.setattr(wrapper_mod, "_resolve_binding", fake_resolve)
    return captured


# ---------------------------------------------------------------------------
# wrap_google_agent — clone + non-mutation
# ---------------------------------------------------------------------------


def test_wrap_google_agent_returns_a_new_agent_with_wrapped_tools(
    resolved: dict[str, Any],
) -> None:
    """Returns a clone whose tools are policy-gated copies, plus the binding."""
    original = _make_agent()

    wrapped, binding = wrap_google_agent(original, api_key="k")

    assert wrapped is not original
    assert wrapped.name == original.name
    assert len(wrapped.tools) == len(original.tools) == 2
    assert isinstance(binding, PolicyBinding)
    assert resolved["name"] == "my_agent"
    assert resolved["key"] == "k"


def test_wrap_google_agent_does_not_mutate_original_agent(
    resolved: dict[str, Any],
) -> None:
    """The original agent's tool list is left untouched after wrapping."""
    original = _make_agent()
    original_tools = list(original.tools)

    wrap_google_agent(original, api_key="k")

    assert list(original.tools) == original_tools


def test_wrap_google_agent_with_no_tools_returns_clone_with_empty_tools(
    resolved: dict[str, Any],
) -> None:
    original = _make_agent(with_tools=False)

    wrapped, _ = wrap_google_agent(original, api_key="k")

    assert wrapped is not original
    assert list(wrapped.tools) == []


def test_wrap_shares_one_enforcer_between_tools_and_binding(
    resolved: dict[str, Any],
) -> None:
    """The binding's enforcer is the one the gated tools consult — a
    refresh swap reaches every tool with no re-wrapping."""
    _, binding = wrap_google_agent(_make_agent(), api_key="k")

    assert isinstance(binding.enforcer, PolicyEnforcer)
    assert binding.enforcer.agent_name == "my_agent"


# ---------------------------------------------------------------------------
# wrap_google_agent — the RESOLVED policy is what's enforced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrap_enforces_the_resolved_policy_not_allow_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deny-by-default engine from resolve actually blocks the tools."""
    monkeypatch.setattr(
        wrapper_mod,
        "_resolve_binding",
        lambda agent, name, key: _stub_binding(
            _engine({"default_policy": {"mode": "deny"}})
        ),
    )

    wrapped, _ = wrap_google_agent(_make_agent(), api_key="api-123")

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
    role_aware = PolicySet(
        {
            DEFAULT_ROLE_NAME: AgentPolicy.model_validate(
                {"default_policy": {"mode": "deny"}}
            ),
            "support": AgentPolicy.model_validate(
                {
                    "default_policy": {"mode": "deny"},
                    "tools": {
                        "echo": {"mode": "allow"},
                        "shout": {"mode": "allow"},
                    },
                }
            ),
        }
    )
    monkeypatch.setattr(
        wrapper_mod,
        "_resolve_binding",
        lambda agent, name, key: _stub_binding(role_aware),
    )

    wrapped, _ = wrap_google_agent(_make_agent(), api_key="k")
    [echo_tool, _shout] = wrapped.tools

    # No User → deny.
    denied = await echo_tool.run_async(args={"text": "hi"}, tool_context=None)
    assert "policy_denied" in denied

    # support → allow.
    async with User(user_id="u-1", role="support"):
        allowed = await echo_tool.run_async(args={"text": "hi"}, tool_context=None)
    assert allowed == "echo:hi"


@pytest.mark.asyncio
async def test_refresh_swap_reaches_already_wrapped_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebinding enforcer.policy (what refresh does) flips live decisions."""
    monkeypatch.setattr(
        wrapper_mod,
        "_resolve_binding",
        lambda agent, name, key: _stub_binding(
            _engine({"default_policy": {"mode": "deny"}})
        ),
    )
    wrapped, binding = wrap_google_agent(_make_agent(), api_key="k")
    [echo_tool, _] = wrapped.tools

    denied = await echo_tool.run_async(args={"text": "hi"}, tool_context=None)
    assert "policy_denied" in denied

    binding.enforcer.policy = _allow_all(["echo", "shout"])  # the refresh swap

    allowed = await echo_tool.run_async(args={"text": "hi"}, tool_context=None)
    assert allowed == "echo:hi"


# ---------------------------------------------------------------------------
# _resolve_binding — 404 → register → retry; everything else loud
# ---------------------------------------------------------------------------


def test_404_registers_adk_agent_then_resolves_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fortify.cli.register as register_pkg

    calls: list[str] = []
    registered: list[Any] = []
    stub = _stub_binding(_allow_all(["echo"]))

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
    binding = wrapper_mod._resolve_binding(agent, "my_agent", "k")

    assert binding is stub
    assert calls == ["my_agent", "my_agent"]
    assert registered == [agent]  # the introspectable ADK object itself


def test_non_404_failure_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrapping asked for governance — a platform error never yields a
    silently allow-all agent."""

    def fake_resolve(name: str, *, api_key: str | None = None, client: Any = None):
        raise FortifyError("Fortify API error 500 calling …", status=500)

    monkeypatch.setattr(
        wrapper_mod.PolicyBinding, "resolve", staticmethod(fake_resolve)
    )

    with pytest.raises(FortifyError, match="500"):
        wrap_google_agent(_make_agent(), api_key="k")
