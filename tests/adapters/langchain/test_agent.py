"""Tests for the FortifyLangchainAgent proxy."""

from __future__ import annotations

from typing import Any, AsyncIterator, Iterator

import pytest

from fortify.adapters.langchain import tools as langchain_tools
from fortify.adapters.langchain.agent import FortifyLangchainAgent
from fortify.security import AgentPolicy
from fortify.user_context import UserContext


def _user_context() -> UserContext:
    """Build a minimal UserContext for invocation tests."""
    return UserContext(user_id="u-1", session_id="s-1", user_role="developer")


class _RecordingGraph:
    """Capture the active policy and config seen by each invocation method."""

    name = "recording-graph"

    def __init__(self) -> None:
        """Initialize empty capture slots."""
        self.invoke_calls: list[dict[str, Any]] = []
        self.ainvoke_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.astream_calls: list[dict[str, Any]] = []
        self.astream_events_calls: list[dict[str, Any]] = []

    def _snapshot(self, payload: dict[str, Any], config: Any) -> dict[str, Any]:
        """Capture the active policy plus call arguments."""
        return {
            "policy": langchain_tools._active_policy.get(),
            "input": payload,
            "config": config,
        }

    def invoke(
        self, payload: dict[str, Any], config: Any, **_kwargs: Any
    ) -> dict[str, Any]:
        """Record sync invocation arguments."""
        self.invoke_calls.append(self._snapshot(payload, config))
        return {"messages": ["sync-ok"]}

    async def ainvoke(
        self, payload: dict[str, Any], config: Any, **_kwargs: Any
    ) -> dict[str, Any]:
        """Record async invocation arguments."""
        self.ainvoke_calls.append(self._snapshot(payload, config))
        return {"messages": ["async-ok"]}

    def stream(
        self, payload: dict[str, Any], config: Any, **_kwargs: Any
    ) -> Iterator[dict[str, Any]]:
        """Yield two chunks while exposing the active policy via capture."""
        self.stream_calls.append(self._snapshot(payload, config))
        yield {"chunk": 1}
        yield {"chunk": 2}

    async def astream(
        self, payload: dict[str, Any], config: Any, **_kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        """Async-yield two chunks."""
        self.astream_calls.append(self._snapshot(payload, config))
        yield {"chunk": 1}
        yield {"chunk": 2}

    async def astream_events(
        self,
        payload: dict[str, Any],
        version: str,
        *,
        config: Any,
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async-yield two events, also recording the requested version."""
        snapshot = self._snapshot(payload, config)
        snapshot["version"] = version
        self.astream_events_calls.append(snapshot)
        yield {"event": "start"}
        yield {"event": "end"}

    def some_attribute(self) -> str:
        """Expose an arbitrary attribute used to verify __getattr__ delegation."""
        return "delegated"


def _stub_build_agent_policy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace policy resolution with a deterministic stub.

    Returns a dict that captures every call and the policy returned, so
    tests can assert on what was forwarded into the active context.
    """
    captured: dict[str, Any] = {"calls": []}

    def fake_build(
        api_key: str,
        context: UserContext,
        agent_name: str,
        tool_names: list[str],
    ) -> AgentPolicy:
        policy = AgentPolicy.model_validate(
            {
                "default_policy": {"mode": "deny"},
                "tools": {name: {"mode": "allow"} for name in tool_names},
            }
        )
        captured["calls"].append(
            {
                "api_key": api_key,
                "context": context,
                "agent_name": agent_name,
                "tool_names": list(tool_names),
                "returned_policy": policy,
            }
        )
        return policy

    monkeypatch.setattr(
        "fortify.adapters.langchain.agent.build_agent_policy", fake_build
    )
    return captured


def test_with_callbacks_appends_handler_to_empty_config() -> None:
    """Add the Fortify handler when no config is provided."""
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(agent=graph, api_key="k", tool_names=[])

    merged = proxy._with_callbacks(None)

    assert proxy._callback_handler in merged["callbacks"]
    assert len(merged["callbacks"]) == 1


def test_with_callbacks_preserves_existing_callbacks() -> None:
    """Append the handler after any pre-existing callbacks."""
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(agent=graph, api_key="k", tool_names=[])
    sentinel_callback = object()

    merged = proxy._with_callbacks({"callbacks": [sentinel_callback]})

    assert merged["callbacks"][0] is sentinel_callback
    assert merged["callbacks"][-1] is proxy._callback_handler


def test_with_callbacks_does_not_double_register_handler() -> None:
    """Skip re-adding the handler when it is already present."""
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(agent=graph, api_key="k", tool_names=[])

    merged_once = proxy._with_callbacks(None)
    merged_twice = proxy._with_callbacks(merged_once)

    assert merged_twice["callbacks"].count(proxy._callback_handler) == 1


def test_invoke_binds_active_policy_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind the policy for the call and forward to the underlying agent."""
    captured = _stub_build_agent_policy(monkeypatch)
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(
        agent=graph, api_key="api-key-123", tool_names=["echo"]
    )

    assert langchain_tools._active_policy.get() is None

    result = proxy.invoke({"input": "hi"}, user_context=_user_context())

    assert result == {"messages": ["sync-ok"]}
    assert len(graph.invoke_calls) == 1
    call = graph.invoke_calls[0]
    assert call["policy"] is captured["calls"][0]["returned_policy"]
    assert call["input"] == {"input": "hi"}
    assert proxy._callback_handler in call["config"]["callbacks"]
    assert langchain_tools._active_policy.get() is None


def test_invoke_forwards_user_context_into_policy_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass api_key, user_context, agent name, and tool names to build_agent_policy."""
    captured = _stub_build_agent_policy(monkeypatch)
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(
        agent=graph, api_key="api-key-123", tool_names=["echo", "search"]
    )
    ctx = _user_context()

    proxy.invoke({"input": "hi"}, user_context=ctx)

    [policy_call] = captured["calls"]
    assert policy_call["api_key"] == "api-key-123"
    assert policy_call["context"] is ctx
    assert policy_call["agent_name"] == "recording-graph"
    assert policy_call["tool_names"] == ["echo", "search"]


def test_invoke_uses_default_agent_name_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fall back to 'default' when the wrapped graph exposes no name."""
    captured = _stub_build_agent_policy(monkeypatch)

    class NamelessGraph:
        def invoke(
            self, payload: dict[str, Any], config: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            return {"ok": True}

    proxy = FortifyLangchainAgent(agent=NamelessGraph(), api_key="k", tool_names=[])

    proxy.invoke({"input": "hi"}, user_context=_user_context())

    assert captured["calls"][0]["agent_name"] == "default"


@pytest.mark.asyncio
async def test_ainvoke_binds_active_policy_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async invocation binds and delegates the same way as sync invoke."""
    captured = _stub_build_agent_policy(monkeypatch)
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(agent=graph, api_key="k", tool_names=["echo"])

    result = await proxy.ainvoke({"input": "hi"}, user_context=_user_context())

    assert result == {"messages": ["async-ok"]}
    [call] = graph.ainvoke_calls
    assert call["policy"] is captured["calls"][0]["returned_policy"]
    assert proxy._callback_handler in call["config"]["callbacks"]
    assert langchain_tools._active_policy.get() is None


def test_stream_binds_active_policy_and_yields_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync stream binds the policy and yields every chunk produced by the agent."""
    captured = _stub_build_agent_policy(monkeypatch)
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(agent=graph, api_key="k", tool_names=["echo"])

    chunks = list(proxy.stream({"input": "hi"}, user_context=_user_context()))

    assert chunks == [{"chunk": 1}, {"chunk": 2}]
    [call] = graph.stream_calls
    assert call["policy"] is captured["calls"][0]["returned_policy"]
    assert proxy._callback_handler in call["config"]["callbacks"]
    assert langchain_tools._active_policy.get() is None


@pytest.mark.asyncio
async def test_astream_binds_active_policy_and_yields_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async stream binds the policy and yields every chunk."""
    captured = _stub_build_agent_policy(monkeypatch)
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(agent=graph, api_key="k", tool_names=["echo"])

    chunks = [
        chunk
        async for chunk in proxy.astream({"input": "hi"}, user_context=_user_context())
    ]

    assert chunks == [{"chunk": 1}, {"chunk": 2}]
    [call] = graph.astream_calls
    assert call["policy"] is captured["calls"][0]["returned_policy"]
    assert proxy._callback_handler in call["config"]["callbacks"]
    assert langchain_tools._active_policy.get() is None


@pytest.mark.asyncio
async def test_astream_events_forwards_version_and_binds_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """astream_events binds the policy and passes the requested version through."""
    captured = _stub_build_agent_policy(monkeypatch)
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(agent=graph, api_key="k", tool_names=["echo"])

    events = [
        evt
        async for evt in proxy.astream_events(
            {"input": "hi"}, "v2", user_context=_user_context()
        )
    ]

    assert events == [{"event": "start"}, {"event": "end"}]
    [call] = graph.astream_events_calls
    assert call["version"] == "v2"
    assert call["policy"] is captured["calls"][0]["returned_policy"]
    assert proxy._callback_handler in call["config"]["callbacks"]
    assert langchain_tools._active_policy.get() is None


def test_active_policy_is_unset_when_invoke_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tear down the active policy even when the wrapped agent raises."""
    _stub_build_agent_policy(monkeypatch)

    class BoomGraph:
        name = "boom"

        def invoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("boom")

    proxy = FortifyLangchainAgent(agent=BoomGraph(), api_key="k", tool_names=[])

    with pytest.raises(RuntimeError, match="boom"):
        proxy.invoke({"input": "hi"}, user_context=_user_context())

    assert langchain_tools._active_policy.get() is None


def test_proxy_delegates_unknown_attributes_to_wrapped_agent() -> None:
    """__getattr__ should fall through to the wrapped CompiledStateGraph."""
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(agent=graph, api_key="k", tool_names=[])

    assert proxy.some_attribute() == "delegated"
    assert proxy.name == "recording-graph"
