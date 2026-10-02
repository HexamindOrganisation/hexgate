"""Guards surfaced on the manifest (PR1 of the platform-guards work).

Covers the ``Guard`` → :class:`GuardManifest` mapping (:func:`_to_guard_manifest`)
and its attachment in :func:`create_manifest`: position, reach, observe, and the
official-vs-custom display hint. The None-default (not ``[]``) keeps the manifest
hash unchanged for every agent that declares no guards.
"""

from __future__ import annotations

import logging

from hexgate.guards import (
    ToolCall,
    after_tool,
    attach_guards,
    before_tool,
    read_guards,
)
from hexgate.guards.attach import GUARDS_ATTR
from hexgate.guards.types import Guard
from hexgate.manifest import create_manifest
from hexgate.manifest.builder import _to_guard_manifest
from hexgate.manifest.models import MAX_GUARDS
from hexgate.plugins import secret_guard, secret_watch


def _openai_agent():
    """A minimal OpenAI agent — the cheapest real agent for create_manifest()."""
    from agents import Agent

    return Agent(name="test-agent", instructions="A test agent", tools=[])


# --- _to_guard_manifest: the Guard -> GuardManifest mapping --------------------


def test_before_guard_maps_to_before_position():
    @before_tool
    def my_guard(call: ToolCall) -> None:
        return None

    gm = _to_guard_manifest(my_guard)
    assert gm.name == "my_guard"
    assert gm.position == "before"
    assert gm.tool_names is None
    assert gm.observe is False
    assert gm.kind == "custom"
    assert gm.plugin_id is None


def test_after_observe_guard_maps_to_after_position():
    @after_tool(observe=True)
    def watcher(call: ToolCall, outcome) -> None:
        return None

    gm = _to_guard_manifest(watcher)
    assert gm.position == "after"
    assert gm.observe is True
    assert gm.kind == "custom"


def test_scoped_guard_records_sorted_tool_names():
    @before_tool(tool_names=["refund_order", "charge_card"])
    def scoped(call: ToolCall) -> None:
        return None

    gm = _to_guard_manifest(scoped)
    # frozenset reach is emitted deterministically (sorted) so the hash is stable.
    assert gm.tool_names == ["charge_card", "refund_order"]


def test_official_before_guard_carries_plugin_id():
    gm = _to_guard_manifest(secret_guard)
    assert gm.name == "secret_guard"
    assert gm.position == "before"
    assert gm.kind == "official"
    assert gm.plugin_id == "secret_guard"


def test_official_after_observe_guard():
    gm = _to_guard_manifest(secret_watch)
    assert gm.name == "secret_watch"
    assert gm.position == "after"
    assert gm.observe is True
    assert gm.kind == "official"
    assert gm.plugin_id == "secret_watch"


def test_module_none_is_not_official():
    """A callable whose __module__ is None (a C/builtin) must not crash and is custom."""

    class _NoModule:
        __module__ = None  # type: ignore[assignment]
        __name__ = "builtin_like"

        def __call__(self, call: ToolCall) -> None:
            return None

    guard = Guard(fn=_NoModule(), position="pre")
    gm = _to_guard_manifest(guard)
    assert gm.kind == "custom"
    assert gm.plugin_id is None


def test_plugins_prefix_lookalike_is_not_official():
    """A module that merely starts with 'hexgate.plugins' is custom, not built-in."""

    def local_guard(call: ToolCall) -> None:
        return None

    local_guard.__module__ = "hexgate.plugins_local"
    gm = _to_guard_manifest(Guard(fn=local_guard, position="pre"))
    assert gm.kind == "custom"
    assert gm.plugin_id is None


def test_plugins_submodule_is_official():
    """A dotted submodule of hexgate.plugins is a built-in."""

    def sub_guard(call: ToolCall) -> None:
        return None

    sub_guard.__module__ = "hexgate.plugins.secrets"
    gm = _to_guard_manifest(Guard(fn=sub_guard, position="pre"))
    assert gm.kind == "official"
    assert gm.plugin_id == "sub_guard"


def test_lambda_guard_warns_no_stable_name(caplog):
    """An inline lambda has no addressable name; the mapper warns but still emits it."""
    guard = before_tool(lambda call: None)
    with caplog.at_level(logging.WARNING, logger="hexgate.manifest.builder"):
        gm = _to_guard_manifest(guard)
    assert gm.name == "<lambda>"
    assert any("no stable name" in r.message for r in caplog.records)


# --- create_manifest integration ----------------------------------------------


def test_create_manifest_attaches_declared_guards():
    @before_tool(tool_names="send_email")
    def local_guard(call: ToolCall) -> None:
        return None

    manifest = create_manifest(_openai_agent(), guards=[local_guard, secret_watch])
    assert manifest.guards is not None
    assert [g.name for g in manifest.guards] == ["local_guard", "secret_watch"]
    assert manifest.guards[0].tool_names == ["send_email"]
    assert manifest.guards[0].kind == "custom"
    assert manifest.guards[1].kind == "official"


def test_create_manifest_guards_default_none():
    """No guards -> None (not []), so the manifest hash is unchanged."""
    manifest = create_manifest(_openai_agent())
    assert manifest.guards is None


def test_create_manifest_empty_guards_is_none():
    """An empty list is falsy and stays None, matching the no-guards hash."""
    manifest = create_manifest(_openai_agent(), guards=[])
    assert manifest.guards is None


# --- attach_guards / read_guards: guards travel on the agent object -----------


def test_attach_and_read_roundtrip():
    @before_tool
    def g(call: ToolCall) -> None:
        return None

    agent = _openai_agent()
    returned = attach_guards(agent, [g])
    assert returned is agent  # chainable
    assert read_guards(agent) == (g,)


def test_attach_empty_is_noop():
    agent = _openai_agent()
    attach_guards(agent, None)
    attach_guards(agent, [])
    assert read_guards(agent) is None
    assert not hasattr(agent, GUARDS_ATTR)


def test_stamp_excluded_from_manifest_relevant_serialization():
    """On an OpenAI Agent the stamp is an instance attr, not a dataclass field,
    so it never rides into a dataclasses.replace / re-serialization."""
    import dataclasses

    @before_tool
    def g(call: ToolCall) -> None:
        return None

    agent = attach_guards(_openai_agent(), [g])
    clone = dataclasses.replace(agent, tools=[])
    assert read_guards(clone) is None  # not a field → dropped by replace, as designed


def test_create_manifest_reads_stamped_guards_off_agent():
    """The CLI path: no explicit guards=, guards are read off the stamped agent."""

    @before_tool(tool_names="send_email")
    def local_guard(call: ToolCall) -> None:
        return None

    agent = attach_guards(_openai_agent(), [local_guard, secret_watch])
    manifest = create_manifest(agent)  # no guards= — mirrors `hexgate register`
    assert manifest.guards is not None
    assert [g.name for g in manifest.guards] == ["local_guard", "secret_watch"]


def test_explicit_guards_override_the_stamp():
    """An explicit guards= wins over whatever is stamped on the agent."""

    @before_tool
    def stamped(call: ToolCall) -> None:
        return None

    @before_tool
    def explicit(call: ToolCall) -> None:
        return None

    agent = attach_guards(_openai_agent(), [stamped])
    manifest = create_manifest(agent, guards=[explicit])
    assert [g.name for g in manifest.guards] == ["explicit"]


def test_explicit_empty_overrides_stamp_to_none():
    """guards=[] explicitly means 'no guards', overriding a stamp."""

    @before_tool
    def stamped(call: ToolCall) -> None:
        return None

    agent = attach_guards(_openai_agent(), [stamped])
    manifest = create_manifest(agent, guards=[])
    assert manifest.guards is None


def test_create_agent_auto_stamps_native_agent(monkeypatch):
    """create_agent(guards=...) stamps the returned agent, so `hexgate register`
    surfaces the guards with no explicit list."""
    from hexgate.agents import factory

    # Stub the graph build + handler and clear governance env, so construction is
    # hermetic (no model provider, no Langfuse, no policy bind) — we only care that
    # the returned agent carries the guards.
    monkeypatch.setattr(factory, "create_langchain_agent", lambda **kw: "graph")
    monkeypatch.setattr(factory, "get_langfuse_handler", lambda **kw: "handler")
    for var in (
        "HEXGATE_API_KEY",
        "HEXGATE_LOCAL_POLICY",
        "HEXGATE_BIND_AGENTS",
        "HEXGATE_LOCAL_MODE",
    ):
        monkeypatch.delenv(var, raising=False)

    @before_tool
    def native_guard(call: ToolCall) -> None:
        return None

    agent, _ = factory.create_agent(
        "m", tools=[], name="guarded", guards=[native_guard]
    )
    assert read_guards(agent) == (native_guard,)
    manifest = create_manifest(agent)  # no guards= — the CLI path
    assert manifest.guards is not None
    assert [g.name for g in manifest.guards] == ["native_guard"]


def test_max_guards_cap_is_effective(caplog):
    """The cap runs where guards are assigned (the builder), since a field_validator
    would not fire on post-construction assignment."""

    @before_tool
    def g(call: ToolCall) -> None:
        return None

    many = [g] * (MAX_GUARDS + 5)
    with caplog.at_level(logging.WARNING):
        manifest = create_manifest(_openai_agent(), guards=many)
    assert manifest.guards is not None
    assert len(manifest.guards) == MAX_GUARDS
    assert any("guards" in r.message and "cap" in r.message for r in caplog.records)


# --- attach_guards works on every registered object type (finding: coverage) ---


def _named_guard():
    @before_tool
    def g(call: ToolCall) -> None:
        return None

    return g


def test_attach_read_google_llmagent():
    """Google ADK LlmAgent is a pydantic model with extra='forbid'; the private
    stamp must set, read back, and stay out of model_dump (so the hash is safe)."""
    from google.adk.agents import LlmAgent

    g = _named_guard()
    agent = LlmAgent(name="g_agent", model="gemini-2.0-flash")
    attach_guards(agent, [g])
    assert read_guards(agent) == (g,)
    assert GUARDS_ATTR not in agent.model_dump()
    # end-to-end CLI path: create_manifest surfaces the stamped guard
    manifest = create_manifest(agent)
    assert [gm.name for gm in (manifest.guards or [])] == ["g"]


def test_attach_read_langgraph_compiled_graph():
    """A raw LangGraph CompiledStateGraph must accept and return the stamp."""
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class _S(TypedDict):
        x: int

    builder = StateGraph(_S)
    builder.add_node("n", lambda s: s)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    graph = builder.compile()

    g = _named_guard()
    attach_guards(graph, [g])
    assert read_guards(graph) == (g,)


def test_attach_read_pydantic_ai_agent():
    """A Pydantic-AI Agent (plain object) must accept and return the stamp."""
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    g = _named_guard()
    agent = Agent(TestModel(), name="p-agent")
    attach_guards(agent, [g])
    assert read_guards(agent) == (g,)
    manifest = create_manifest(agent, description="d")
    assert [gm.name for gm in (manifest.guards or [])] == ["g"]


# --- resolve_guards: the shared explicit-else-stamp rule -----------------------


def test_resolve_guards_prefers_explicit_else_stamp():
    """resolve_guards returns the explicit list when given, else the agent's stamp;
    an explicit [] overrides the stamp to 'no guards' (review #6)."""
    from hexgate.guards.attach import resolve_guards

    stamped = _named_guard()
    explicit = _named_guard()
    agent = attach_guards(_openai_agent(), [stamped])

    assert list(resolve_guards(agent, [explicit])) == [explicit]  # explicit wins
    assert list(resolve_guards(agent, None)) == [stamped]  # falls back to stamp
    assert list(resolve_guards(agent, []) or []) == []  # explicit empty overrides


def test_resolve_guards_none_on_unstamped_agent():
    """No stamp and no explicit list → None (a caller treats 'no guards' uniformly)."""
    from hexgate.guards.attach import resolve_guards

    assert resolve_guards(_openai_agent(), None) is None


# --- clone preservation: a cloned agent keeps running its guards ---------------


def test_clone_preserves_stamped_guards():
    """OpenAI Agent.clone() rebuilds from fields and drops the plain stamp; since the
    runtime now reads the stamp, attach_guards wraps clone so a clone re-stamps and
    doesn't silently run unguarded (review #3)."""
    g = _named_guard()
    agent = attach_guards(_openai_agent(), [g])

    clone = agent.clone(name="cloned-agent")

    assert clone.name == "cloned-agent"
    assert read_guards(clone) == (g,)
    # ...and a clone of the clone stays guarded (the wrap is recursive).
    assert read_guards(clone.clone()) == (g,)
