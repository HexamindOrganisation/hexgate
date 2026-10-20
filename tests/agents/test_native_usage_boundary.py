"""The native agent's platform seam: which client its enforcer is built from, and
the run-start usage fetch its binding drives.

The LangChain graph build and the Langfuse handler are stubbed as
tests/agents/test_create_agent_binding.py does; the usage source is a spy.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

import pytest
from langchain_core.tools import tool

from hexgate.agents import factory
from hexgate.cloud.client import HexgateClient, HexgateConfig
from hexgate.guards import before_tool
from hexgate.runtime.agent_usage import USAGE_LEDGERS, UsageScope
from hexgate.security import AgentPolicy, BaseToolPolicy, PolicySet
from hexgate.security import enforcer as enforcer_mod
from hexgate.security.policy_set import DEFAULT_ROLE_NAME, load_policy_set_from_dict
from tests.agents.helpers import run_once

_KEY = "fty_test_demo_dummybiscuit"
_AGENT = "bot"


@tool
def echo(text: str) -> str:
    """Echo the input back."""
    return text


@before_tool
def passthrough(_call: Any) -> None:
    """A guard that lets every call through."""
    return None


def _platform_client(base_url: str = "https://other.example") -> HexgateClient:
    return HexgateClient(HexgateConfig(base_url=base_url, api_key=_KEY))


def _allow_echo() -> PolicySet:
    return PolicySet(
        {DEFAULT_ROLE_NAME: AgentPolicy(tools={"echo": BaseToolPolicy(mode="allow")})}
    )


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        factory, "create_langchain_agent", lambda **kwargs: "graph-instance"
    )
    monkeypatch.setattr(
        factory, "get_langfuse_handler", lambda **kwargs: "handler-instance"
    )
    for name in (
        "HEXGATE_API_KEY",
        "HEXGATE_LOCAL_POLICY",
        "HEXGATE_BIND_AGENTS",
        "HEXGATE_LOCAL_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    # Audit is not under test; a real sender would outlive the test.
    monkeypatch.setattr(enforcer_mod, "configure", lambda *_a, **_k: None)


@pytest.fixture
def source_clients(monkeypatch: pytest.MonkeyPatch) -> list[HexgateClient | None]:
    """The client each ``resolve_usage_source`` call was handed."""
    seen: list[HexgateClient | None] = []

    def spy(*, api_key: str | None = None, client: Any = None) -> None:
        seen.append(client)

    monkeypatch.setattr(enforcer_mod, "resolve_usage_source", spy)
    return seen


def _agent() -> factory.HexgateAgent:
    agent, _ = factory.create_agent(
        model="openai:gpt-5.4", tools=[echo], name=_AGENT, bind_policy=False
    )
    return agent


def test_enforce_policy_defaults_to_the_attached_client(
    source_clients: list[HexgateClient | None],
) -> None:
    attached = _platform_client()
    agent = _agent()
    agent.hexgate_client = attached

    enforced = agent.enforce_policy(_allow_echo())

    assert source_clients == [attached]
    assert enforced.hexgate_client is attached


def test_an_explicit_client_beats_the_attached_one_and_is_attached(
    source_clients: list[HexgateClient | None],
) -> None:
    explicit = _platform_client("https://explicit.example")
    agent = _agent()
    agent.hexgate_client = _platform_client()

    enforced = agent.enforce_policy(_allow_echo(), client=explicit)

    assert source_clients == [explicit]
    assert enforced.hexgate_client is explicit


def test_the_guards_only_path_attaches_the_client() -> None:
    explicit = _platform_client()

    enforced = _agent().enforce_policy(None, guards=[passthrough], client=explicit)

    assert enforced._binding is None
    assert enforced.hexgate_client is explicit


def test_the_module_alias_forwards_the_client(
    source_clients: list[HexgateClient | None],
) -> None:
    explicit = _platform_client()

    enforced = factory.enforce_policy(_agent(), _allow_echo(), client=explicit)

    assert source_clients == [explicit]
    assert enforced.hexgate_client is explicit


_GRAPH_RAN = "graph"
_CAPPED_PATH = "tool_calls_5m"


class _RecordingSource:
    """A usage source that records each refresh into the shared event log."""

    def __init__(self, events: list[object]) -> None:
        self._events = events

    def refresh(self, scope: UsageScope, paths: frozenset[str]) -> None:
        self._events.append(("refresh", scope, set(paths)))

    async def refresh_async(self, scope: UsageScope, paths: frozenset[str]) -> None:
        self._events.append(("refresh", scope, set(paths)))


class _RecordingGraph:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    async def ainvoke(self, payload: dict, config: Any = None) -> dict:
        self._events.append(_GRAPH_RAN)
        return {"messages": ["ok"]}

    async def astream_events(
        self, payload: dict, config: Any = None, version: str = "v2"
    ) -> Any:
        self._events.append(_GRAPH_RAN)
        yield {"event": "one"}


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """The ordered log of usage refreshes and graph runs."""
    log: list[object] = []
    graph = _RecordingGraph(log)
    monkeypatch.setattr(factory, "create_langchain_agent", lambda **kwargs: graph)
    monkeypatch.setattr(
        enforcer_mod, "resolve_usage_source", lambda **_: _RecordingSource(log)
    )
    # A usage policy enables the process ledger registry for good; restore it.
    monkeypatch.setattr(USAGE_LEDGERS, "_enabled", frozenset())
    monkeypatch.setattr(USAGE_LEDGERS, "_agent_ledgers", {})
    monkeypatch.setattr(USAGE_LEDGERS, "_user_ledgers", OrderedDict())
    monkeypatch.setattr(USAGE_LEDGERS, "_user_capacity", USAGE_LEDGERS._user_capacity)
    return log


def _tool_cap() -> PolicySet:
    return load_policy_set_from_dict(
        {
            "tools": {
                "echo": {
                    "mode": "allow",
                    "constraints": [f"agent_usage.{_CAPPED_PATH} < 3"],
                }
            }
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["ainvoke", "astream_events"])
async def test_a_frozen_policy_refreshes_usage_before_the_graph_runs(
    events: list[object], method: str
) -> None:
    """No refresh source, yet the binding still drives the run-start fetch: the
    seam user quotas rely on for native agents."""
    agent = _agent().enforce_policy(_tool_cap())

    await run_once(agent, method)

    assert events == [("refresh", UsageScope.agent(_AGENT), {_CAPPED_PATH}), _GRAPH_RAN]


@pytest.mark.asyncio
async def test_a_usage_free_policy_does_not_fetch(events: list[object]) -> None:
    agent = _agent().enforce_policy(_allow_echo())

    await run_once(agent, "ainvoke")

    assert events == [_GRAPH_RAN]


@pytest.mark.asyncio
async def test_guards_only_agent_has_no_binding_and_runs(
    events: list[object],
) -> None:
    agent = _agent().enforce_policy(None, guards=[passthrough])

    await run_once(agent, "ainvoke")

    assert agent._binding is None
    assert events == [_GRAPH_RAN]
