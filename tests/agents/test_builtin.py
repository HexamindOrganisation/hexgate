"""Tests for packaged builtin agents."""

from __future__ import annotations

from typing import Any

import pytest

from hexgate.agents import loader
from hexgate.agents.models import AgentSpec


def test_list_builtin_agents_includes_researcher() -> None:
    """List the packaged builtin agent directories."""
    assert "researcher" in loader.list_builtin_agents()


def test_load_builtin_agent_spec_reads_packaged_yaml() -> None:
    """Load the packaged researcher agent spec."""
    spec = loader.load_builtin_agent_spec("researcher")

    assert isinstance(spec, AgentSpec)
    assert spec.name == "researcher"
    assert spec.tools == ["web_search", "fetch"]
    assert spec.policy == "policy.yaml"


def test_load_builtin_agent_policy_reads_packaged_policy() -> None:
    """Load the packaged researcher policy."""
    policy = loader.load_builtin_agent_policy("researcher")

    assert policy.default_policy.mode == "deny"
    assert policy.tools["web_search"].mode == "allow"
    assert policy.tools["fetch"].mode == "allow"


def test_resolve_builtin_tools_raises_for_unknown_tools() -> None:
    """Fail clearly when a referenced tool id is unknown."""
    with pytest.raises(KeyError, match='Unknown tool "missing_tool"'):
        loader.resolve_builtin_tools(["missing_tool"])


def test_load_builtin_agent_resolves_spec_into_create_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Instantiate a builtin agent by wiring prompt, tools, and policy."""
    captured: dict[str, Any] = {}
    captured_policy: dict[str, Any] = {}

    def fake_create_agent(**kwargs: Any) -> tuple[str, str]:
        """Capture builtin loader kwargs and return fake instances."""
        captured.update(kwargs)
        return "agent-instance", "handler-instance"

    def fake_enforce_policy(
        tools: list[Any], policy: Any, *, approval_handler: Any = None
    ) -> list[Any]:
        """Capture policy application while leaving tools unchanged."""
        captured_policy["policy"] = policy
        captured_policy["approval_handler"] = approval_handler
        return tools

    monkeypatch.setattr(loader, "create_agent", fake_create_agent)
    monkeypatch.setattr(loader, "enforce_policy", fake_enforce_policy)

    agent, handler = loader.load_builtin_agent("researcher", session_id="s-1")

    assert (agent, handler) == ("agent-instance", "handler-instance")
    assert captured["name"] == "researcher"
    assert captured["session_id"] == "s-1"
    assert captured["model"] == "gpt-5.4"
    assert [tool.name for tool in captured["tools"]] == ["web_search", "fetch"]
    assert "web research assistant" in captured["system_prompt"]
    assert captured_policy["policy"].tools["web_search"].mode == "allow"
