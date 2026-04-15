"""Tests for policy loading and enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.tools import tool

from coolagents.agent import factory
from coolagents.agent.security import enforce_policy, with_before_action
from coolagents.security import (
    AgentPolicy,
    ApprovalRequiredError,
    PolicyDeniedError,
    authorize_tool_call,
    load_policy,
)


def test_load_policy_reads_yaml_file(tmp_path: Path) -> None:
    """Parse a YAML policy file into an AgentPolicy model."""
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "\n".join(
            [
                "version: 1",
                "default_policy:",
                "  mode: deny",
                "tools:",
                "  web_search:",
                "    mode: allow",
            ]
        ),
        encoding="utf-8",
    )

    policy = load_policy(policy_path)

    assert isinstance(policy, AgentPolicy)
    assert policy.tools["web_search"].mode == "allow"
    assert policy.default_policy.mode == "deny"


def test_authorize_tool_call_respects_allow_and_default_deny() -> None:
    """Allow explicit tool entries and deny others by default."""
    policy = AgentPolicy.model_validate(
        {
            "default_policy": {"mode": "deny"},
            "tools": {"web_search": {"mode": "allow"}},
        }
    )

    authorize_tool_call(policy, "web_search")

    with pytest.raises(PolicyDeniedError, match='Policy denied tool "fetch"'):
        authorize_tool_call(policy, "fetch")


def test_authorize_tool_call_requires_approval_when_configured() -> None:
    """Raise a distinct error for approval-gated tools."""
    policy = AgentPolicy.model_validate(
        {
            "default_policy": {"mode": "deny"},
            "tools": {"write_file": {"mode": "approval_required"}},
        }
    )

    with pytest.raises(ApprovalRequiredError, match='approval for tool "write_file"'):
        authorize_tool_call(policy, "write_file")


@pytest.mark.asyncio
async def test_enforce_policy_denies_tool_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap created agents so denied invocations fail before execution."""

    @tool
    async def sample_tool(value: str) -> str:
        """Return a transformed string."""
        return value.upper()

    monkeypatch.setattr(factory, "create_langchain_agent", lambda **_kwargs: object())
    monkeypatch.setattr(factory, "get_langfuse_handler", lambda **_kwargs: "handler")

    agent, _handler = factory.create_agent(
        model="openai:gpt-5.4",
        tools=[sample_tool],
        system_prompt="You are a test assistant.",
    )

    secured_agent = enforce_policy(
        agent,
        AgentPolicy.model_validate(
            {"default_policy": {"mode": "deny"}, "tools": {"sample_tool": {"mode": "deny"}}}
        ),
    )

    with pytest.raises(PolicyDeniedError, match='Policy denied tool "sample_tool"'):
        await secured_agent.tools[0].ainvoke({"value": "hello"})


@pytest.mark.asyncio
async def test_with_before_action_receives_action_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the hosted pre-tool hook before the actual tool call."""

    @tool
    async def sample_tool(value: str) -> str:
        """Return a transformed string."""
        return value.upper()

    monkeypatch.setattr(factory, "create_langchain_agent", lambda **_kwargs: object())
    monkeypatch.setattr(factory, "get_langfuse_handler", lambda **_kwargs: "handler")

    seen: dict[str, object] = {}

    async def before_action(action: dict[str, object], context: dict[str, object] | None) -> None:
        seen["action"] = action
        seen["context"] = context

    agent, _handler = factory.create_agent(
        model="openai:gpt-5.4",
        tools=[sample_tool],
        system_prompt="You are a test assistant.",
        name="sample-agent",
    )

    guarded_agent = with_before_action(
        agent,
        before_action,
        context_provider=lambda: {"tenant_id": "acme"},
    )

    result = await guarded_agent.tools[0].ainvoke({"value": "hello"})

    assert result == "HELLO"
    assert seen["action"] == {
        "tool_name": "sample_tool",
        "arguments": {"value": "hello"},
        "agent_name": "sample-agent",
    }
    assert seen["context"] == {"tenant_id": "acme"}


@pytest.mark.asyncio
async def test_with_before_action_can_block_tool_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Let the hosted pre-tool hook abort execution by raising."""

    @tool
    async def sample_tool(value: str) -> str:
        """Return a transformed string."""
        return value.upper()

    monkeypatch.setattr(factory, "create_langchain_agent", lambda **_kwargs: object())
    monkeypatch.setattr(factory, "get_langfuse_handler", lambda **_kwargs: "handler")

    def before_action(_action: dict[str, object], _context: dict[str, object] | None) -> None:
        raise RuntimeError("blocked by host platform")

    agent, _handler = factory.create_agent(
        model="openai:gpt-5.4",
        tools=[sample_tool],
        system_prompt="You are a test assistant.",
    )

    guarded_agent = with_before_action(agent, before_action)

    with pytest.raises(RuntimeError, match="blocked by host platform"):
        await guarded_agent.tools[0].ainvoke({"value": "hello"})
