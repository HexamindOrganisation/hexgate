"""Tests for the LangChain adapter wrapper entry point."""

from __future__ import annotations

import pytest
from langchain_core.tools import BaseTool, tool

from fortify.adapters.langchain.agent import FortifyLangchainAgent
from fortify.adapters.langchain.wrapper import build_policy_set, wrap_langchain_agent
from fortify.security import PolicySet


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


# ---------------------------------------------------------------------------
# build_policy_set (stub today, allow-all per tool)
# ---------------------------------------------------------------------------


def test_build_policy_set_allows_each_tool_under_default_role() -> None:
    """The placeholder policy builder allows every tool name it receives."""
    policy_set = build_policy_set("k", "agent-name", ["echo", "shout"])

    assert isinstance(policy_set, PolicySet)
    default_policy = policy_set.policy_for(None)
    assert default_policy.tools["echo"].mode == "allow"
    assert default_policy.tools["shout"].mode == "allow"


def test_build_policy_set_with_no_tools_returns_empty_tools_map() -> None:
    policy_set = build_policy_set("k", "agent-name", [])

    assert policy_set.policy_for(None).tools == {}


# ---------------------------------------------------------------------------
# wrap_langchain_agent — API key resolution
# ---------------------------------------------------------------------------


def test_wrap_returns_fortify_proxy_with_supplied_tool_names() -> None:
    graph = _FakeCompiledGraph()
    tools = [_make_tool("a"), _make_tool("b")]

    wrapped = wrap_langchain_agent(agent=graph, tools=tools, api_key="fortify-key")

    assert isinstance(wrapped, FortifyLangchainAgent)
    assert wrapped._tool_names == ["a", "b"]
    assert wrapped._agent is graph
    assert wrapped._api_key == "fortify-key"


def test_wrap_falls_back_to_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORTIFY_KEY", "from-env")

    wrapped = wrap_langchain_agent(agent=_FakeCompiledGraph(), tools=[])

    assert wrapped._api_key == "from-env"


def test_wrap_prefers_explicit_api_key_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORTIFY_KEY", "from-env")

    wrapped = wrap_langchain_agent(
        agent=_FakeCompiledGraph(), tools=[], api_key="explicit"
    )

    assert wrapped._api_key == "explicit"


def test_wrap_raises_when_no_api_key_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FORTIFY_KEY", raising=False)

    with pytest.raises(ValueError, match="No API key provided"):
        wrap_langchain_agent(agent=_FakeCompiledGraph(), tools=[])


def test_wrap_treats_empty_api_key_string_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORTIFY_KEY", "")

    with pytest.raises(ValueError, match="No API key provided"):
        wrap_langchain_agent(agent=_FakeCompiledGraph(), tools=[], api_key="")


# ---------------------------------------------------------------------------
# wrap_langchain_agent — enforcer installation
# ---------------------------------------------------------------------------


def test_wrap_installs_enforcer_on_each_tool_in_place() -> None:
    """Every tool gets the install marker — graph keeps its references."""
    tools = [_make_tool("a"), _make_tool("b")]

    wrap_langchain_agent(agent=_FakeCompiledGraph(), tools=tools, api_key="k")

    for t in tools:
        assert getattr(t, "_fortify_enforcer_installed") is True
        assert t.handle_tool_error is True


def test_wrap_is_idempotent_on_already_wrapped_tools() -> None:
    """Re-wrapping rebinds the enforcer; doesn't stack gates."""
    tools = [_make_tool("a")]
    wrap_langchain_agent(agent=_FakeCompiledGraph(), tools=tools, api_key="k")
    first_func = tools[0].func

    wrap_langchain_agent(agent=_FakeCompiledGraph(), tools=tools, api_key="k")

    # New closure replaced the previous one — but the original is preserved
    # under _fortify_original_func, so behavior stays consistent.
    assert tools[0].func is not first_func
    # Calling still works (default stub allows everything).
    assert tools[0].func(text="hi") == "echo:hi"


def test_wrap_passes_through_with_empty_tool_list() -> None:
    wrapped = wrap_langchain_agent(
        agent=_FakeCompiledGraph(), tools=[], api_key="fortify-key"
    )

    assert wrapped._tool_names == []
