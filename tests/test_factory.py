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
        self.astream_calls: list[dict[str, Any]] = []

    async def ainvoke(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Record an invoke call and return a fake response."""
        self.ainvoke_calls.append({"payload": payload, "config": config})
        return {"messages": ["ok"]}

    async def astream(self, payload: dict[str, Any], config: dict[str, Any], stream_mode: str):
        """Yield two fake streamed events."""
        self.astream_calls.append(
            {"payload": payload, "config": config, "stream_mode": stream_mode}
        )
        yield ("chunk-1", {"node": "model"})
        yield ("chunk-2", {"node": "model"})


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
async def test_stream_agent_uses_message_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stream agent events with the messages stream mode."""
    fake_agent = FakeAgent()
    monkeypatch.setattr(
        factory,
        "get_langfuse_runnable_config",
        lambda handler: {"callbacks": [handler]},
    )

    events = [event async for event in factory.stream_agent(fake_agent, "handler", "hello")]

    assert events == [("chunk-1", {"node": "model"}), ("chunk-2", {"node": "model"})]
    assert fake_agent.astream_calls == [
        {
            "payload": {"messages": [{"role": "user", "content": "hello"}]},
            "config": {"callbacks": ["handler"]},
            "stream_mode": "messages",
        }
    ]
