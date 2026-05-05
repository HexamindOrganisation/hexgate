"""Tests for policy loading and enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.tools import tool

from fortify.agent import factory
from fortify.agent.security import (
    enforce_policy,
    with_approval_handler,
    with_before_action,
)
from fortify.security import (
    AgentPolicy,
    ApprovalRequiredError,
    BaseToolPolicy,
    FileToolPolicy,
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


def test_agent_policy_parses_file_tool_policy_scope() -> None:
    """Parse file-specific scope fields into the specialized file policy model."""
    policy = AgentPolicy.model_validate(
        {
            "default_policy": {"mode": "deny"},
            "tools": {
                "read_file": {
                    "mode": "allow",
                    "file_scope": {
                        "allowed_paths": ["docs/**"],
                        "denied_paths": ["secrets/**"],
                    },
                }
            },
        }
    )

    tool_policy = policy.tools["read_file"]

    assert isinstance(tool_policy, FileToolPolicy)
    assert tool_policy.file_scope is not None
    assert tool_policy.file_scope.allowed_paths == ["docs/**"]
    assert tool_policy.file_scope.denied_paths == ["secrets/**"]
    assert isinstance(policy.default_policy, BaseToolPolicy)


def test_authorize_tool_call_denies_out_of_scope_file_path() -> None:
    """Deny file-tool calls when the requested path falls outside the allowlist."""
    policy = AgentPolicy.model_validate(
        {
            "default_policy": {"mode": "deny"},
            "tools": {
                "read_file": {
                    "mode": "allow",
                    "file_scope": {"allowed_paths": ["docs/**"]},
                }
            },
        }
    )

    authorize_tool_call(policy, "read_file", {"file_path": "docs/guide.md"})

    with pytest.raises(PolicyDeniedError, match="requested path"):
        authorize_tool_call(policy, "read_file", {"file_path": "notes/todo.md"})


def test_authorize_tool_call_denied_paths_override_allowed_paths() -> None:
    """Let explicit denied paths win even when an allowlist would otherwise match."""
    policy = AgentPolicy.model_validate(
        {
            "default_policy": {"mode": "deny"},
            "tools": {
                "edit_file": {
                    "mode": "approval_required",
                    "file_scope": {
                        "allowed_paths": ["docs/**"],
                        "denied_paths": ["docs/private/**"],
                    },
                }
            },
        }
    )

    with pytest.raises(ApprovalRequiredError):
        authorize_tool_call(policy, "edit_file", {"file_path": "docs/report.md"})

    with pytest.raises(PolicyDeniedError, match="requested path"):
        authorize_tool_call(policy, "edit_file", {"file_path": "docs/private/plan.md"})


def test_authorize_tool_call_denies_scoped_search_without_explicit_path() -> None:
    """Treat scoped search tools without a path anchor as out of scope."""
    policy = AgentPolicy.model_validate(
        {
            "default_policy": {"mode": "deny"},
            "tools": {
                "grep": {
                    "mode": "allow",
                    "file_scope": {"allowed_paths": ["docs/**"]},
                }
            },
        }
    )

    with pytest.raises(PolicyDeniedError, match="requested path"):
        authorize_tool_call(policy, "grep", {"pattern": "Napoleon"})


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
async def test_enforce_policy_denies_tool_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrap created agents so denied invocations become graceful tool results."""

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
            {
                "default_policy": {"mode": "deny"},
                "tools": {"sample_tool": {"mode": "deny"}},
            }
        ),
    )

    result = await secured_agent.tools[0].ainvoke({"value": "hello"})

    assert result == {
        "ok": False,
        "error": {
            "type": "policy_denied",
            "message": 'Policy denied tool "sample_tool"',
            "tool_name": "sample_tool",
            "retryable": False,
        },
    }


@pytest.mark.asyncio
async def test_enforce_policy_includes_file_scope_hint_for_out_of_scope_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Include allowed/denied path hints when a file tool is blocked by file scope."""

    @tool
    async def read_file(file_path: str) -> str:
        """Read a file."""
        return file_path

    monkeypatch.setattr(factory, "create_langchain_agent", lambda **_kwargs: object())
    monkeypatch.setattr(factory, "get_langfuse_handler", lambda **_kwargs: "handler")

    agent, _handler = factory.create_agent(
        model="openai:gpt-5.4",
        tools=[read_file],
        system_prompt="You are a test assistant.",
    )

    secured_agent = enforce_policy(
        agent,
        AgentPolicy.model_validate(
            {
                "default_policy": {"mode": "deny"},
                "tools": {
                    "read_file": {
                        "mode": "allow",
                        "file_scope": {
                            "allowed_paths": ["docs/**"],
                            "denied_paths": ["docs/private/**"],
                        },
                    }
                },
            }
        ),
    )

    result = await secured_agent.tools[0].ainvoke({"file_path": "notes/todo.md"})

    assert result == {
        "ok": False,
        "error": {
            "type": "policy_denied",
            "message": 'Policy denied tool "read_file" for the requested path',
            "tool_name": "read_file",
            "retryable": False,
            "hint": {
                "allowed_paths": ["docs/**"],
                "denied_paths": ["docs/private/**"],
            },
        },
    }


@pytest.mark.asyncio
async def test_enforce_policy_approval_required_defaults_to_graceful_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat approval-required tools like denied ones when no handler is configured."""

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
            {
                "default_policy": {"mode": "deny"},
                "tools": {"sample_tool": {"mode": "approval_required"}},
            }
        ),
    )

    result = await secured_agent.tools[0].ainvoke({"value": "hello"})

    assert result == {
        "ok": False,
        "error": {
            "type": "approval_required",
            "message": 'Tool "sample_tool" requires approval before execution',
            "tool_name": "sample_tool",
            "retryable": False,
        },
    }


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

    async def before_action(
        action: dict[str, object], context: dict[str, object] | None
    ) -> None:
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
    """Convert hosted vetoes into graceful tool results."""

    @tool
    async def sample_tool(value: str) -> str:
        """Return a transformed string."""
        return value.upper()

    monkeypatch.setattr(factory, "create_langchain_agent", lambda **_kwargs: object())
    monkeypatch.setattr(factory, "get_langfuse_handler", lambda **_kwargs: "handler")

    def before_action(
        _action: dict[str, object], _context: dict[str, object] | None
    ) -> None:
        raise RuntimeError("blocked by host platform")

    agent, _handler = factory.create_agent(
        model="openai:gpt-5.4",
        tools=[sample_tool],
        system_prompt="You are a test assistant.",
    )

    guarded_agent = with_before_action(agent, before_action)

    result = await guarded_agent.tools[0].ainvoke({"value": "hello"})

    assert result == {
        "ok": False,
        "error": {
            "type": "before_action_denied",
            "message": "blocked by host platform",
            "tool_name": "sample_tool",
            "retryable": False,
        },
    }


@pytest.mark.asyncio
async def test_with_approval_handler_can_allow_approval_required_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow approval-required tools when the host approval handler returns True."""

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
            {
                "default_policy": {"mode": "deny"},
                "tools": {"sample_tool": {"mode": "approval_required"}},
            }
        ),
    )
    approved_agent = with_approval_handler(secured_agent, True)

    result = await approved_agent.tools[0].ainvoke({"value": "hello"})

    assert result == "HELLO"


@pytest.mark.asyncio
async def test_with_approval_handler_supports_async_host_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use a host callback to make approval decisions at runtime."""

    @tool
    async def sample_tool(value: str) -> str:
        """Return a transformed string."""
        return value.upper()

    monkeypatch.setattr(factory, "create_langchain_agent", lambda **_kwargs: object())
    monkeypatch.setattr(factory, "get_langfuse_handler", lambda **_kwargs: "handler")

    seen: dict[str, object] = {}

    async def approval_handler(
        action: dict[str, object],
        context: dict[str, object] | None,
    ) -> bool:
        seen["action"] = action
        seen["context"] = context
        return False

    agent, _handler = factory.create_agent(
        model="openai:gpt-5.4",
        tools=[sample_tool],
        system_prompt="You are a test assistant.",
        name="sample-agent",
    )

    secured_agent = enforce_policy(
        agent,
        AgentPolicy.model_validate(
            {
                "default_policy": {"mode": "deny"},
                "tools": {"sample_tool": {"mode": "approval_required"}},
            }
        ),
    )
    approval_agent = with_approval_handler(
        secured_agent,
        approval_handler,
        context_provider=lambda: {"tenant_id": "acme"},
    )

    result = await approval_agent.tools[0].ainvoke({"value": "hello"})

    assert result == {
        "ok": False,
        "error": {
            "type": "approval_required",
            "message": 'Tool "sample_tool" requires approval before execution',
            "tool_name": "sample_tool",
            "retryable": False,
        },
    }
    assert seen["action"] == {
        "tool_name": "sample_tool",
        "arguments": {"value": "hello"},
        "agent_name": "sample-agent",
    }
    assert seen["context"] == {"tenant_id": "acme"}


@pytest.mark.asyncio
async def test_enforced_tool_emits_single_tool_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avoid nested duplicate tool runs when policy wrapping is applied."""

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
            {
                "default_policy": {"mode": "deny"},
                "tools": {"sample_tool": {"mode": "allow"}},
            }
        ),
    )

    event_names: list[str] = []
    async for event in secured_agent.tools[0].astream_events(
        {"value": "hello"}, version="v2"
    ):
        name = event.get("event")
        if isinstance(name, str):
            event_names.append(name)

    assert event_names.count("on_tool_start") == 1
    assert event_names.count("on_tool_end") == 1
