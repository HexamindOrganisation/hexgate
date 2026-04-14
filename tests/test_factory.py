"""Tests for agent factory helpers."""

from __future__ import annotations

from typing import Any

import pytest

from asianf.agent import factory
from asianf.config.settings import Settings


class FakeAgent:
    """Provide a tiny async agent for factory tests."""

    def __init__(self) -> None:
        """Initialize call tracking for the fake agent."""
        self.ainvoke_calls: list[dict[str, Any]] = []
        self.astream_event_calls: list[dict[str, Any]] = []

    async def ainvoke(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Record an invoke call and return a fake response."""
        self.ainvoke_calls.append({"payload": payload, "config": config})
        return {"messages": ["ok"]}

    async def astream_events(
        self,
        payload: dict[str, Any],
        config: dict[str, Any],
        *,
        version: str,
    ):
        """Yield two fake raw LangChain stream events."""
        self.astream_event_calls.append(
            {"payload": payload, "config": config, "version": version}
        )
        yield {"event": "one"}
        yield {"event": "two"}


def make_settings() -> Settings:
    """Build a valid settings object for tests."""
    return Settings(
        openai_api_key="openai-key",
        linkup_api_key="linkup-key",
        tavily_api_key="tavily-key",
        langfuse_public_key="public-key",
        langfuse_secret_key="secret-key",
        langfuse_host="https://cloud.langfuse.com",
        model="openai:gpt-5.4",
        search_engine="linkup",
    )


def test_load_system_prompt_reads_prompt_file() -> None:
    """Load the markdown system prompt text from disk."""
    prompt = factory._load_system_prompt()

    assert "web research assistant" in prompt
    assert "web_search" in prompt
    assert "fetch" in prompt


def test_create_agent_wires_tools_and_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Create the LangChain agent with the expected tools and prompt."""
    calls: dict[str, Any] = {}

    def fake_create_langchain_agent(**kwargs: Any) -> str:
        """Capture the agent creation kwargs."""
        calls["agent_kwargs"] = kwargs
        return "agent-instance"

    def fake_get_langfuse_handler(**kwargs: Any) -> str:
        """Capture the handler creation kwargs."""
        calls["handler_kwargs"] = kwargs
        return "handler-instance"

    monkeypatch.setattr(factory, "create_langchain_agent", fake_create_langchain_agent)
    monkeypatch.setattr(factory, "get_langfuse_handler", fake_get_langfuse_handler)

    agent, handler = factory.create_agent(
        make_settings(),
        session_id="session-1",
        user_id="user-1",
    )

    assert agent == "agent-instance"
    assert handler == "handler-instance"
    assert calls["agent_kwargs"]["model"] == "openai:gpt-5.4"
    assert calls["agent_kwargs"]["tools"] == [factory.web_search, factory.fetch]
    assert "web research assistant" in calls["agent_kwargs"]["system_prompt"]
    assert calls["handler_kwargs"] == {
        "session_id": "session-1",
        "user_id": "user-1",
        "tags": ["asianf", "linkup", "openai:gpt-5.4"],
    }


@pytest.mark.asyncio
async def test_invoke_agent_passes_messages_and_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invoke the agent with the expected message payload."""
    fake_agent = FakeAgent()
    monkeypatch.setattr(
        factory,
        "get_langfuse_runnable_config",
        lambda handler: {"callbacks": [handler]},
    )

    result = await factory.invoke_agent(fake_agent, "handler", "hello")

    assert result == {"messages": ["ok"]}
    assert fake_agent.ainvoke_calls == [
        {
            "payload": {"messages": [{"role": "user", "content": "hello"}]},
            "config": {"callbacks": ["handler"]},
        }
    ]


@pytest.mark.asyncio
async def test_stream_agent_raw_uses_astream_events_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stream raw agent events through LangChain's event stream API."""
    fake_agent = FakeAgent()
    monkeypatch.setattr(
        factory,
        "get_langfuse_runnable_config",
        lambda handler: {"callbacks": [handler]},
    )
    monkeypatch.setattr(factory, "new_root_run_id", lambda: "run-123")

    events = [event async for event in factory.stream_agent_raw(fake_agent, "handler", "hello")]

    assert events == [{"event": "one"}, {"event": "two"}]
    assert fake_agent.astream_event_calls == [
        {
            "payload": {"messages": [{"role": "user", "content": "hello"}]},
            "config": {"callbacks": ["handler"], "run_id": "run-123"},
            "version": "v2",
        }
    ]


@pytest.mark.asyncio
async def test_stream_agent_normalizes_raw_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normalize raw LangChain events into app-level stream events."""

    async def fake_stream_agent_raw(agent: Any, handler: Any, query: str):
        """Yield a small fake raw event sequence."""
        assert agent == "agent"
        assert handler == "handler"
        assert query == "hello"
        yield {"event": "one"}
        yield {"event": "two"}

    async def fake_normalize(raw_events: Any, *, query: str):
        """Yield normalized events from the fake raw stream."""
        assert query == "hello"
        collected = [event async for event in raw_events]
        assert collected == [{"event": "one"}, {"event": "two"}]
        yield {"normalized": 1}
        yield {"normalized": 2}

    monkeypatch.setattr(factory, "stream_agent_raw", fake_stream_agent_raw)
    monkeypatch.setattr(factory, "normalize_langchain_events", fake_normalize)

    events = [event async for event in factory.stream_agent("agent", "handler", "hello")]

    assert events == [{"normalized": 1}, {"normalized": 2}]
