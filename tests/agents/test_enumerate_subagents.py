"""Tests for ``enumerate_subagents`` — per-framework sub-agent introspection.

Asserts the discovered ``(target, via)`` set for each framework the reader knows
(HexgateAgent registry, OpenAI handoffs/as-tool, Google sub_agents/AgentTool) and
that closured / unknown agents yield ``[]``. Pure read — no policy, no behavior.
"""

from __future__ import annotations

import pytest

from hexgate.adapters.langchain.tools import SubagentTool
from hexgate.agents import factory
from hexgate.agents.enumeration import SubagentLink, enumerate_subagents
from hexgate.security.naming import canonical_name


def _links(agent: object) -> set[tuple[str, str]]:
    return {(link.target, link.via) for link in enumerate_subagents(agent)}


# --- HexgateAgent (native / the agent-as-tool construct) -------------------


class _FakeChild:
    def __init__(self, name: str = "billing_bot") -> None:
        self.name = name

    async def ainvoke(self, payload: dict, config: dict) -> dict:
        return {"messages": []}


def _tool(child: _FakeChild) -> SubagentTool:
    """Mount ``child`` as an agent-as-tool edge (what ``child.as_tool()`` builds)."""
    return SubagentTool(
        name="delegate",
        description="d",
        child=child,
        target_name=canonical_name(child.name),
    )


@pytest.fixture()
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the graph build + handler and clear governance env for construction."""
    monkeypatch.setattr(
        factory, "create_langchain_agent", lambda **kwargs: "graph-instance"
    )
    monkeypatch.setattr(
        factory, "get_langfuse_handler", lambda **kwargs: "handler-instance"
    )
    for var in (
        "HEXGATE_API_KEY",
        "HEXGATE_LOCAL_POLICY",
        "HEXGATE_BIND_AGENTS",
        "HEXGATE_LOCAL_MODE",
    ):
        monkeypatch.delenv(var, raising=False)


def test_hexgate_agent_reads_subagents_registry(_hermetic: None) -> None:
    child = _FakeChild("billing_bot")
    parent, _ = factory.create_agent("m", tools=[_tool(child)], name="parent")
    links = enumerate_subagents(parent)
    assert links == [SubagentLink("billing_bot", "tool", child)]


def test_hexgate_agent_without_subagents_is_empty(_hermetic: None) -> None:
    parent, _ = factory.create_agent("m", tools=[], name="parent")
    assert enumerate_subagents(parent) == []


def test_non_canonical_child_name_is_canonicalized(_hermetic: None) -> None:
    parent, _ = factory.create_agent(
        "m", tools=[_tool(_FakeChild(" billing_bot "))], name="parent"
    )
    assert _links(parent) == {("billing_bot", "tool")}


# --- closures / unknown → [] -----------------------------------------------


def test_unknown_object_yields_no_links() -> None:
    assert enumerate_subagents(object()) == []


def test_link_is_hashable_over_target_and_via() -> None:
    # `child` is excluded from hash/eq, so a link carrying an unhashable framework
    # agent is still hashable and dedups by (target, via) — what PR4 relies on.
    class _Unhashable:
        __hash__ = None  # type: ignore[assignment]

    a = SubagentLink("billing_bot", "tool", _Unhashable())
    b = SubagentLink("billing_bot", "tool", _Unhashable())
    assert a == b and hash(a) == hash(b)
    assert {a, b} == {a}  # set dedup does not raise on the unhashable child


def test_local_agents_package_object_without_sdk_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A foreign object whose module string looks like OpenAI's ("agents...") but the
    # SDK import fails must honor the [] contract, not raise ImportError.
    import builtins

    class _Foreign:
        pass

    _Foreign.__module__ = "agents.mypackage"
    real_import = builtins.__import__

    def _no_agents(name: str, *args: object, **kwargs: object) -> object:
        if name == "agents" or name.startswith("agents."):
            raise ImportError("no openai-agents SDK here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_agents)
    assert enumerate_subagents(_Foreign()) == []


# --- OpenAI Agents ----------------------------------------------------------


def test_openai_handoffs_and_as_tool() -> None:
    agents = pytest.importorskip("agents")
    from agents.tool import ToolOrigin, ToolOriginType

    async def _on_invoke(_ctx: object, _raw: str) -> str:
        return "ok"

    as_tool = agents.FunctionTool(
        name="consult_refunds",
        description="reach the refunds sub-agent as a tool",
        params_json_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        on_invoke_tool=_on_invoke,
        _tool_origin=ToolOrigin(
            type=ToolOriginType.AGENT_AS_TOOL, agent_name="refunds"
        ),
        _is_agent_tool=True,
    )
    plain = agents.FunctionTool(
        name="echo",
        description="an ordinary tool",
        params_json_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        on_invoke_tool=_on_invoke,
    )
    parent = agents.Agent(
        name="parent",
        handoffs=[agents.Agent(name="billing_bot")],
        tools=[as_tool, plain],
    )
    assert _links(parent) == {("billing_bot", "handoff"), ("refunds", "tool")}


def test_openai_agent_without_edges_is_empty() -> None:
    agents = pytest.importorskip("agents")
    assert enumerate_subagents(agents.Agent(name="lonely")) == []


def test_openai_bare_handoff_descriptor_recovers_live_child() -> None:
    agents = pytest.importorskip("agents")
    child = agents.Agent(name="billing_bot")
    parent = agents.Agent(name="parent", handoffs=[agents.handoff(child)])
    (link,) = enumerate_subagents(parent)
    assert (link.target, link.via) == ("billing_bot", "handoff")
    assert link.child is child  # recovered from Handoff._agent_ref, so PR4 can recurse


def test_openai_nameless_handoff_maps_to_default() -> None:
    agents = pytest.importorskip("agents")
    parent = agents.Agent(name="parent", handoffs=[agents.Agent(name="")])
    assert _links(parent) == {("default", "handoff")}  # not dropped; matches the gate


# --- Google ADK -------------------------------------------------------------


def test_google_sub_agents_and_agent_tool() -> None:
    pytest.importorskip("google.adk")
    from google.adk.agents import LlmAgent
    from google.adk.tools.agent_tool import AgentTool

    parent = LlmAgent(
        name="parent",
        model="gemini-2.0-flash",
        sub_agents=[LlmAgent(name="billing_bot", model="gemini-2.0-flash")],
        tools=[AgentTool(agent=LlmAgent(name="refunds", model="gemini-2.0-flash"))],
    )
    assert _links(parent) == {("billing_bot", "handoff"), ("refunds", "tool")}


def test_google_agent_without_edges_is_empty() -> None:
    pytest.importorskip("google.adk")
    from google.adk.agents import LlmAgent

    assert enumerate_subagents(LlmAgent(name="lonely", model="gemini-2.0-flash")) == []
