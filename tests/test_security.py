"""Tests for policy loading and enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.tools import tool

from coolagents.agent import factory
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
async def test_create_agent_policy_wrapper_denies_tool_invocation() -> None:
    """Wrap BaseTool instances so denied invocations fail before execution."""

    @tool
    async def sample_tool(value: str) -> str:
        """Return a transformed string."""
        return value.upper()

    [wrapped] = factory._wrap_tools_with_policy(  # pyright: ignore[reportPrivateUsage]
        [sample_tool],
        AgentPolicy.model_validate(
            {"default_policy": {"mode": "deny"}, "tools": {"sample_tool": {"mode": "deny"}}}
        ),
    )

    with pytest.raises(PolicyDeniedError, match='Policy denied tool "sample_tool"'):
        await wrapped.ainvoke({"value": "hello"})
