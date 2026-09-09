"""Agent-as-tool reach gating on the Google ADK adapter.

An ``AgentTool`` names itself after its wrapped agent, so ``wrap_tool`` gates it
under its reach key ``agent.tool:<target>`` (the same substitution the OpenAI
adapter uses) instead of by tool name — otherwise the transfer plugin *and* the
name gate would both decide one delegation, and the documented `via: tool` grant
would be overridden by the default-deny name gate.
"""

from __future__ import annotations

from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool

from hexgate.adapters.google.tools import _agent_tool_target, wrap_tool
from hexgate.security import AgentPolicy, PolicySet
from hexgate.security.enforcer import PolicyEnforcer
from hexgate.security.policy_set import DEFAULT_ROLE_NAME


def _enforcer(spec: dict[str, Any]) -> PolicyEnforcer:
    return PolicyEnforcer(
        PolicySet({DEFAULT_ROLE_NAME: AgentPolicy.model_validate(spec)}),
        agent_name="orchestrator",
    )


def _agent_tool(
    target: str = "refund_bot", calls: list[Any] | None = None
) -> AgentTool:
    """An ``AgentTool`` whose ``run_async`` is stubbed (the real one runs the
    sub-agent through a model)."""
    record: list[Any] = calls if calls is not None else []

    class _StubAgentTool(AgentTool):
        async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
            record.append(args)
            return "ran-subagent"

    return _StubAgentTool(agent=LlmAgent(name=target, model="gemini-2.0-flash"))


async def _run(tool: AgentTool) -> Any:
    return await tool.run_async(args={"request": "refund"}, tool_context=None)


def test_detects_agent_tool_target() -> None:
    assert _agent_tool_target(_agent_tool(target="refund_bot")) == "refund_bot"


@pytest.mark.asyncio
async def test_documented_example_via_tool_allow_is_gated_by_reach_key() -> None:
    """The doc example: default deny + ``refund_bot: {via: [tool], allow}``. The
    AgentTool is *not* in ``tools:``, so a name gate would deny it under the default
    — the reach-key substitution is what makes the documented grant work."""
    calls: list[Any] = []
    enforcer = _enforcer(
        {
            "default_policy": {"mode": "deny"},
            "agents": {"refund_bot": {"via": ["tool"], "mode": "allow"}},
        }
    )
    wrapped = wrap_tool(_agent_tool(calls=calls), enforcer)

    result = await _run(wrapped)

    assert result == "ran-subagent"
    assert calls  # the sub-agent ran — the via:tool grant was honored


@pytest.mark.asyncio
async def test_closed_world_denies_unlisted_agent_tool() -> None:
    calls: list[Any] = []
    enforcer = _enforcer(
        {
            "default_policy": {"mode": "deny"},
            "agents": {"other_bot": {"via": ["tool"], "mode": "allow"}},
        }
    )
    wrapped = wrap_tool(_agent_tool(calls=calls), enforcer)

    result = await _run(wrapped)

    assert calls == []
    assert "[policy_denied]" in result
    assert "refund_bot" in result
    assert "agent.tool" not in result  # no synthetic-key leak


@pytest.mark.asyncio
async def test_handoff_only_policy_leaves_agent_tool_name_gated() -> None:
    """Matches OpenAI: no ``via: tool`` target declared → the AgentTool falls back
    to name-gating, so the default-deny denies it (rather than closed-world)."""
    calls: list[Any] = []
    enforcer = _enforcer(
        {
            "default_policy": {"mode": "deny"},
            "agents": {"refund_bot": {"via": ["handoff"], "mode": "allow"}},
        }
    )
    wrapped = wrap_tool(_agent_tool(calls=calls), enforcer)

    result = await _run(wrapped)

    assert calls == []
    assert "[policy_denied]" in result
    # name-gated: message names the tool, not a bare reach target
    assert "refund_bot" in result


@pytest.mark.asyncio
async def test_handoff_only_via_tool_grant_allows_by_name() -> None:
    """A handoff-only ``agents`` policy that also allows the tool by name lets the
    AgentTool through the name-gate fallback (proving it isn't closed-world-denied)."""
    calls: list[Any] = []
    enforcer = _enforcer(
        {
            "default_policy": {"mode": "deny"},
            "agents": {"refund_bot": {"via": ["handoff"], "mode": "allow"}},
            "tools": {"refund_bot": {"mode": "allow"}},
        }
    )
    wrapped = wrap_tool(_agent_tool(calls=calls), enforcer)

    result = await _run(wrapped)

    assert result == "ran-subagent"
    assert calls
