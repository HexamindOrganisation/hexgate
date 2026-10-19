"""`HexgateAgent.enforce_policy(guards=...)` wiring, including the guards-only
path where no policy engine is passed.

The LangChain graph build is stubbed (as tests/agents/test_factory.py does) so
these assert the tool-wrapping, not a real graph.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import tool

from hexgate.adapters.langchain.tools import GuardedTool
from hexgate.agents import factory
from hexgate.agents.factory import HexgateAgent
from hexgate.guards import before_tool
from hexgate.guards.stance import GuardClosedWorldError
from hexgate.guards.types import Halt
from hexgate.security.policy_set import load_policy_set_from_dict


@tool
def echo(text: str) -> str:
    """Echo the input back."""
    return text


@before_tool
def g_keep(call: Any) -> None:
    """A named guard the policy leaves enabled."""
    return None


@before_tool
def g_drop(call: Any) -> None:
    """A named guard the policy disables."""
    return None


def _agent(monkeypatch: pytest.MonkeyPatch, name: str = "bot") -> HexgateAgent:
    monkeypatch.setattr(factory, "create_langchain_agent", lambda **k: "graph")
    return HexgateAgent(
        graph="graph", model="m", tools=[echo], system_prompt=None, name=name
    )


def _guards() -> list:
    return [before_tool(lambda call: Halt(reason="blocked"))]


def test_enforce_policy_none_with_guards_wraps_tools_guards_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """policy=None + guards still guards each tool via guards (no enforcer)."""
    agent = _agent(monkeypatch)

    guarded = agent.enforce_policy(None, guards=_guards())

    wrapped = guarded.tools[0]
    assert isinstance(wrapped, GuardedTool)
    assert wrapped.enforcer is None
    assert wrapped.pipeline is not None
    assert len(wrapped.pipeline.pre) == 1


def test_guards_only_path_keeps_approval_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guard Halt(NEEDS_APPROVAL) must be approvable on the guards-only path."""
    agent = _agent(monkeypatch)

    guarded = agent.enforce_policy(None, guards=_guards(), approval_handler=True)

    wrapped = guarded.tools[0]
    assert isinstance(wrapped, GuardedTool)
    assert wrapped.enforcer is None
    assert wrapped.approval_handler is True


def test_enforce_policy_none_without_guards_stays_unguarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch)
    rebuilt = agent.enforce_policy(None)
    assert not isinstance(rebuilt.tools[0], GuardedTool)


def test_enforce_policy_none_empty_guards_stays_unguarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch)
    rebuilt = agent.enforce_policy(None, guards=[])
    assert not isinstance(rebuilt.tools[0], GuardedTool)


def test_enforce_policy_with_policy_and_guards_wraps_with_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hexgate.security import AgentPolicy, PolicySet
    from hexgate.security.policy_set import DEFAULT_ROLE_NAME

    agent = _agent(monkeypatch)
    policy: Any = PolicySet(
        {
            DEFAULT_ROLE_NAME: AgentPolicy.model_validate(
                {
                    "default_policy": {"mode": "deny"},
                    "tools": {"echo": {"mode": "allow"}},
                }
            )
        }
    )

    guarded = agent.enforce_policy(policy, guards=_guards())

    wrapped = guarded.tools[0]
    assert isinstance(wrapped, GuardedTool)
    assert wrapped.pipeline is not None
    assert wrapped.enforcer is not None


def _stub_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory, "create_langchain_agent", lambda **k: "graph")
    monkeypatch.setattr(factory, "get_langfuse_handler", lambda **k: "handler")


def test_create_agent_guards_only_when_no_policy_binds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_agent(guards=..., bind_policy=False) still wraps tools with guards."""
    _stub_build(monkeypatch)

    agent, _ = factory.create_agent(
        model="m", tools=[echo], bind_policy=False, guards=_guards()
    )

    wrapped = agent.tools[0]
    assert isinstance(wrapped, GuardedTool)
    assert wrapped.enforcer is None
    assert wrapped.pipeline is not None


def test_create_agent_binds_policy_and_guards_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the bind path, guards wrap the tools alongside the resolved policy."""
    from dataclasses import dataclass

    from hexgate.security import AgentPolicy, PolicySet
    from hexgate.security.policy_set import DEFAULT_ROLE_NAME

    _stub_build(monkeypatch)
    monkeypatch.setattr(factory, "resolve_api_key", lambda: None)

    engine = PolicySet(
        {
            DEFAULT_ROLE_NAME: AgentPolicy.model_validate(
                {
                    "default_policy": {"mode": "deny"},
                    "tools": {"echo": {"mode": "allow"}},
                }
            )
        }
    )

    @dataclass
    class _Resolved:
        engine: Any
        source: Any = None

    monkeypatch.setattr(
        "hexgate.security.binding.resolve_policy",
        lambda name, client=None: _Resolved(engine=engine),
    )

    agent, _ = factory.create_agent(
        model="m", tools=[echo], name="bot", bind_policy=True, guards=_guards()
    )

    wrapped = agent.tools[0]
    assert isinstance(wrapped, GuardedTool)
    assert wrapped.enforcer is not None
    assert wrapped.pipeline is not None


def test_with_tools_preserves_stamped_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """with_tools underlies enforce_policy/refresh; it must carry the guard stamp
    forward or create_manifest(rebuilt) would publish guards=None while they run
    (review #2)."""
    from hexgate.guards.attach import attach_guards, read_guards

    guards = _guards()
    agent = attach_guards(_agent(monkeypatch), guards)

    rebuilt = agent.with_tools([echo])

    assert read_guards(rebuilt) == tuple(guards)


def test_enforce_policy_falls_back_to_stamped_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enforce_policy(None) with no guards= runs the stamped guards and re-stamps
    the result, so runtime and manifest stay in agreement (review #2)."""
    from hexgate.guards.attach import attach_guards, read_guards

    guards = _guards()
    agent = attach_guards(_agent(monkeypatch), guards)

    guarded = agent.enforce_policy(None)

    wrapped = guarded.tools[0]
    assert isinstance(wrapped, GuardedTool)
    assert wrapped.pipeline is not None
    assert len(wrapped.pipeline.pre) == 1
    assert read_guards(guarded) == tuple(guards)


# --- R-GUARD-007: the stance is applied at run time, not baked into the pipeline --


def test_enforce_installs_the_shared_unfiltered_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The native path installs one shared pipeline with every guard present; a policy
    that disables a guard does NOT prune it here — the runner skips it per call
    (R-GUARD-007). The behavioural drop/override lives in test_guard_policy.py."""
    agent = _agent(monkeypatch)
    policy = load_policy_set_from_dict(
        {"roles": {"default": {"guards": {"g_drop": {"enabled": False}}}}}
    )
    guarded = agent.enforce_policy(policy, guards=[g_keep, g_drop])
    wrapped = guarded.tools[0]
    assert isinstance(wrapped, GuardedTool)
    assert [h.label for h in wrapped.pipeline.pre] == ["g_keep", "g_drop"]


def test_policy_governing_undeclared_guard_stops_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy that toggles a guard the agent never declared raises at construction."""
    agent = _agent(monkeypatch)
    policy = load_policy_set_from_dict(
        {"roles": {"default": {"guards": {"ghost_guard": {"enabled": False}}}}}
    )
    with pytest.raises(GuardClosedWorldError, match="ghost_guard"):
        agent.enforce_policy(policy, guards=[g_keep])


def test_closed_world_fires_when_agent_declares_no_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop-cold check must fire even with no guards attached (no pipeline), the
    'guard deleted from code, policy still references it' case (R-GUARD-007)."""
    agent = _agent(monkeypatch)
    policy = load_policy_set_from_dict(
        {"roles": {"default": {"guards": {"ghost_guard": {"enabled": False}}}}}
    )
    with pytest.raises(GuardClosedWorldError, match="ghost_guard"):
        agent.enforce_policy(policy, guards=None)


def test_closed_world_checks_the_stamped_guards_not_the_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-enforcing a stamped agent without restating guards= must NOT stop cold: the
    check runs against the guards actually installed (the stamp), not the empty
    argument — the feature's main use case (review #1)."""
    from hexgate.guards.attach import attach_guards

    agent = attach_guards(_agent(monkeypatch), [g_keep])
    policy = load_policy_set_from_dict(
        {"roles": {"default": {"guards": {"g_keep": {"enabled": False}}}}}
    )

    guarded = agent.enforce_policy(policy)  # no guards= — falls back to the stamp

    wrapped = guarded.tools[0]
    assert isinstance(wrapped, GuardedTool)
    assert [h.label for h in wrapped.pipeline.pre] == ["g_keep"]


def test_enforcer_and_admission_gate_use_canonical_agent_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch, name=" bot ")
    policy = load_policy_set_from_dict({"roles": {"default": {}}})

    guarded = agent.enforce_policy(policy)

    assert guarded._binding.enforcer.agent_name == "bot"
    assert guarded._agent_gate._enforcer is guarded._binding.enforcer


def test_guard_closed_world_error_names_canonical_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch, name=" bot ")
    policy = load_policy_set_from_dict(
        {"roles": {"default": {"guards": {"ghost_guard": {"enabled": False}}}}}
    )
    with pytest.raises(GuardClosedWorldError, match="agent 'bot'"):
        agent.enforce_policy(policy, guards=[g_keep])
