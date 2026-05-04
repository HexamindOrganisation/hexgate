"""Tests for the pydantic_ai adapter agent wrapper entry point."""

from __future__ import annotations

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import Tool

from fortify.adapters.pydantic_ai.agent import FortifyPydanticAgent
from fortify.adapters.pydantic_ai.wrapper import (
    _clone_agent_with_tools,
    _extract_tools,
    wrap_pydantic_agent,
)


def _make_agent(name: str | None = "my-agent", *, with_tools: bool = True) -> Agent:
    """Build a pydantic_ai Agent backed by a TestModel for wrapper tests."""

    def echo(text: str) -> str:
        """Echo the input back."""
        return f"echo:{text}"

    def shout(text: str) -> str:
        """Shout the input back."""
        return f"SHOUT:{text}"

    tools: list[Tool] = (
        [Tool(echo, name="echo"), Tool(shout, name="shout")] if with_tools else []
    )
    return Agent(TestModel(), name=name, tools=tools)


def test_extract_tools_returns_registered_tools() -> None:
    """Pull every Tool registered on the agent's function toolset."""
    agent = _make_agent()

    extracted = _extract_tools(agent)

    assert sorted(t.name for t in extracted) == ["echo", "shout"]


def test_extract_tools_returns_empty_list_when_no_tools_are_registered() -> None:
    """An agent with no tools yields an empty list."""
    agent = _make_agent(with_tools=False)

    assert _extract_tools(agent) == []


def test_extract_tools_returns_empty_list_when_function_toolset_is_missing() -> None:
    """Tolerate agent-like objects without a `_function_toolset` attribute."""

    class BareAgent:
        """Stand in for an Agent that exposes no toolset."""

    assert _extract_tools(BareAgent()) == []  # type: ignore[arg-type]


def test_clone_agent_with_tools_does_not_mutate_original() -> None:
    """Cloning replaces tools on the copy only — the original keeps its tools."""
    agent = _make_agent()
    original_tool_names = sorted(t.name for t in _extract_tools(agent))

    clone = _clone_agent_with_tools(agent, [])

    assert clone is not agent
    assert clone.instrument is True
    assert clone._function_toolset is not agent._function_toolset
    assert clone._function_toolset.tools == {}
    assert sorted(t.name for t in _extract_tools(agent)) == original_tool_names


def test_clone_agent_with_tools_installs_provided_tools_on_clone() -> None:
    """The clone exposes exactly the wrapped tools, keyed by name."""
    agent = _make_agent()
    [echo_tool, _] = _extract_tools(agent)

    clone = _clone_agent_with_tools(agent, [echo_tool])

    assert list(clone._function_toolset.tools.keys()) == ["echo"]


def test_wrap_pydantic_agent_returns_fortify_proxy() -> None:
    """Return a FortifyPydanticAgent populated with tool names and api key."""
    agent = _make_agent()

    wrapped = wrap_pydantic_agent(agent=agent, api_key="fortify-key")

    assert isinstance(wrapped, FortifyPydanticAgent)
    assert wrapped._api_key == "fortify-key"
    assert wrapped._agent_name == "my-agent"
    assert sorted(wrapped._tool_names) == ["echo", "shout"]


def test_wrap_pydantic_agent_does_not_mutate_original_agent() -> None:
    """Wrapping must clone the agent so the input agent stays usable."""
    agent = _make_agent()
    original_tools = _extract_tools(agent)

    wrapped = wrap_pydantic_agent(agent=agent, api_key="k")

    assert wrapped._agent is not agent
    same_tools = _extract_tools(agent)
    assert {t.name for t in same_tools} == {t.name for t in original_tools}
    for original_tool in original_tools:
        assert original_tool.function_schema is original_tool.function_schema


def test_wrap_pydantic_agent_falls_back_to_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve the API key from FORTIFY_KEY when no explicit key is given."""
    monkeypatch.setenv("FORTIFY_KEY", "from-env")
    agent = _make_agent()

    wrapped = wrap_pydantic_agent(agent=agent)

    assert wrapped._api_key == "from-env"


def test_wrap_pydantic_agent_prefers_explicit_api_key_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit api_key argument wins over FORTIFY_KEY when both are set."""
    monkeypatch.setenv("FORTIFY_KEY", "from-env")

    wrapped = wrap_pydantic_agent(agent=_make_agent(), api_key="explicit")

    assert wrapped._api_key == "explicit"


def test_wrap_pydantic_agent_raises_when_no_api_key_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject construction when neither argument nor env var supplies a key."""
    monkeypatch.delenv("FORTIFY_KEY", raising=False)

    with pytest.raises(ValueError, match="No API key provided"):
        wrap_pydantic_agent(agent=_make_agent())


def test_wrap_pydantic_agent_raises_when_api_key_is_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat an empty FORTIFY_KEY env var the same as missing."""
    monkeypatch.setenv("FORTIFY_KEY", "")

    with pytest.raises(ValueError, match="No API key provided"):
        wrap_pydantic_agent(agent=_make_agent(), api_key="")


def test_wrap_pydantic_agent_uses_default_name_when_agent_has_none() -> None:
    """Fall back to 'default' when the wrapped agent has no `name`."""
    agent = _make_agent(name=None)

    wrapped = wrap_pydantic_agent(agent=agent, api_key="k")

    assert wrapped._agent_name == "default"


def test_wrap_pydantic_agent_clone_has_wrapped_tools() -> None:
    """The cloned agent exposes wrapped (not original) Tool instances."""
    agent = _make_agent()
    original_tools = {t.name: t for t in _extract_tools(agent)}

    wrapped = wrap_pydantic_agent(agent=agent, api_key="k")

    cloned_tools = wrapped._agent._function_toolset.tools
    assert set(cloned_tools.keys()) == set(original_tools.keys())
    for name, cloned_tool in cloned_tools.items():
        assert cloned_tool is not original_tools[name]


def test_wrap_pydantic_agent_with_no_tools() -> None:
    """An agent with no tools wraps cleanly to an empty tool name list."""
    agent = _make_agent(with_tools=False)

    wrapped = wrap_pydantic_agent(agent=agent, api_key="k")

    assert wrapped._tool_names == []
    assert wrapped._agent._function_toolset.tools == {}
