"""The native agent's platform seam: which client its enforcer is built from.

The LangChain graph build and the Langfuse handler are stubbed as
tests/agents/test_create_agent_binding.py does; the usage source is a spy.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import tool

from hexgate.agents import factory
from hexgate.cloud.client import HexgateClient, HexgateConfig
from hexgate.security import AgentPolicy, BaseToolPolicy, PolicySet
from hexgate.security import enforcer as enforcer_mod
from hexgate.security.policy_set import DEFAULT_ROLE_NAME

_KEY = "fty_test_demo_dummybiscuit"
_AGENT = "bot"


@tool
def echo(text: str) -> str:
    """Echo the input back."""
    return text


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
    from hexgate.guards import before_tool

    @before_tool
    def passthrough(_call: Any) -> None:
        return None

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
