"""Tests for the LangChain adapter agent wrapper entry point."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import BaseTool, tool

from fortify.adapters.langchain.agent import FortifyLangchainAgent
from fortify.adapters.langchain.wrapper import wrap_langchain_agent


class _FakeCompiledGraph:
    """Stand in for a CompiledStateGraph during construction-only tests."""

    name = "fake-graph"


def _make_tool(name: str = "echo") -> BaseTool:
    """Create a StructuredTool-style tool for wrapper tests."""

    @tool(name)
    def echo(text: str) -> str:
        """Echo the input back."""
        return f"echo:{text}"

    return echo


def test_wrap_langchain_agent_returns_fortify_proxy() -> None:
    """Return a FortifyLangchainAgent that exposes the supplied tool names."""
    graph = _FakeCompiledGraph()
    tools = [_make_tool("a"), _make_tool("b")]

    wrapped = wrap_langchain_agent(agent=graph, tools=tools, api_key="fortify-key")

    assert isinstance(wrapped, FortifyLangchainAgent)
    assert wrapped._tool_names == ["a", "b"]
    assert wrapped._agent is graph
    assert wrapped._api_key == "fortify-key"


def test_wrap_langchain_agent_installs_policy_gates_on_tools() -> None:
    """Mark every supplied tool as wrapped (in place) by the Fortify gate."""
    tools = [_make_tool("a"), _make_tool("b")]

    wrap_langchain_agent(agent=_FakeCompiledGraph(), tools=tools, api_key="fortify-key")

    for t in tools:
        assert getattr(t, "_fortify_wrapped") is True
        assert t.handle_tool_error is True


def test_wrap_langchain_agent_falls_back_to_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve the API key from FORTIFY_KEY when no explicit key is given."""
    monkeypatch.setenv("FORTIFY_KEY", "from-env")

    wrapped = wrap_langchain_agent(agent=_FakeCompiledGraph(), tools=[])

    assert wrapped._api_key == "from-env"


def test_wrap_langchain_agent_prefers_explicit_api_key_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Let the explicit api_key argument win over FORTIFY_KEY when both are set."""
    monkeypatch.setenv("FORTIFY_KEY", "from-env")

    wrapped = wrap_langchain_agent(
        agent=_FakeCompiledGraph(), tools=[], api_key="explicit"
    )

    assert wrapped._api_key == "explicit"


def test_wrap_langchain_agent_raises_when_no_api_key_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject construction when neither argument nor env var supplies a key."""
    monkeypatch.delenv("FORTIFY_KEY", raising=False)

    with pytest.raises(ValueError, match="No API key provided"):
        wrap_langchain_agent(agent=_FakeCompiledGraph(), tools=[])


def test_wrap_langchain_agent_raises_when_api_key_is_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat an empty FORTIFY_KEY env var the same as missing."""
    monkeypatch.setenv("FORTIFY_KEY", "")

    with pytest.raises(ValueError, match="No API key provided"):
        wrap_langchain_agent(agent=_FakeCompiledGraph(), tools=[], api_key="")


def test_wrap_langchain_agent_passes_through_with_empty_tool_list() -> None:
    """Build a proxy with an empty tool list when the agent has no tools."""
    wrapped = wrap_langchain_agent(
        agent=_FakeCompiledGraph(), tools=[], api_key="fortify-key"
    )

    assert wrapped._tool_names == []


def test_wrap_langchain_agent_is_idempotent_on_already_wrapped_tools() -> None:
    """Re-wrapping an already wrapped tool must not corrupt its func reference."""
    tools = [_make_tool("a")]
    wrap_langchain_agent(agent=_FakeCompiledGraph(), tools=tools, api_key="k")
    first_func: Any = tools[0].func

    wrap_langchain_agent(agent=_FakeCompiledGraph(), tools=tools, api_key="k")

    assert tools[0].func is first_func
