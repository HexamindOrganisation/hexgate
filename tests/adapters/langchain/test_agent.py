"""Tests for the FortifyLangchainAgent proxy."""

from __future__ import annotations

from typing import Any, AsyncIterator, Iterator

import pytest

from fortify.adapters.langchain.agent import FortifyLangchainAgent
from fortify.runtime import User
from fortify.runtime.context import get_current_user


def _user() -> User:
    """Build a minimal User for invocation tests."""
    return User(user_id="u-1", session_id="s-1", role="developer")


class _RecordingGraph:
    """Capture the active User and config seen by each invocation method."""

    name = "recording-graph"

    def __init__(self) -> None:
        self.invoke_calls: list[dict[str, Any]] = []
        self.ainvoke_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.astream_calls: list[dict[str, Any]] = []
        self.astream_events_calls: list[dict[str, Any]] = []

    def _snapshot(self, payload: dict[str, Any], config: Any) -> dict[str, Any]:
        """Capture the active User plus call arguments."""
        return {
            "user": get_current_user(),
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
        """Yield two chunks while exposing the active User via capture."""
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
        """Arbitrary attribute used to verify __getattr__ delegation."""
        return "delegated"


# ---------------------------------------------------------------------------
# Callbacks plumbing
# ---------------------------------------------------------------------------


def test_with_callbacks_appends_handler_to_empty_config() -> None:
    proxy = FortifyLangchainAgent(agent=_RecordingGraph(), api_key="k", tool_names=[])

    merged = proxy._with_callbacks(None)

    assert proxy._callback_handler in merged["callbacks"]
    assert len(merged["callbacks"]) == 1


def test_with_callbacks_preserves_existing_callbacks() -> None:
    proxy = FortifyLangchainAgent(agent=_RecordingGraph(), api_key="k", tool_names=[])
    sentinel = object()

    merged = proxy._with_callbacks({"callbacks": [sentinel]})

    assert merged["callbacks"][0] is sentinel
    assert merged["callbacks"][-1] is proxy._callback_handler


def test_with_callbacks_does_not_double_register_handler() -> None:
    proxy = FortifyLangchainAgent(agent=_RecordingGraph(), api_key="k", tool_names=[])

    merged_once = proxy._with_callbacks(None)
    merged_twice = proxy._with_callbacks(merged_once)

    assert merged_twice["callbacks"].count(proxy._callback_handler) == 1


# ---------------------------------------------------------------------------
# User scope binding per invocation method
# ---------------------------------------------------------------------------


def test_invoke_opens_user_scope_and_delegates() -> None:
    """The active User contextvar is live during the wrapped invoke."""
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(agent=graph, api_key="k", tool_names=["echo"])
    user = _user()

    assert get_current_user() is None

    result = proxy.invoke({"input": "hi"}, user=user)

    assert result == {"messages": ["sync-ok"]}
    [call] = graph.invoke_calls
    assert call["user"] is user
    assert call["input"] == {"input": "hi"}
    assert proxy._callback_handler in call["config"]["callbacks"]
    # Scope unwound after the call.
    assert get_current_user() is None


@pytest.mark.asyncio
async def test_ainvoke_opens_user_scope_and_delegates() -> None:
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(agent=graph, api_key="k", tool_names=["echo"])
    user = _user()

    result = await proxy.ainvoke({"input": "hi"}, user=user)

    assert result == {"messages": ["async-ok"]}
    [call] = graph.ainvoke_calls
    assert call["user"] is user
    assert proxy._callback_handler in call["config"]["callbacks"]
    assert get_current_user() is None


def test_stream_opens_user_scope_and_yields_chunks() -> None:
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(agent=graph, api_key="k", tool_names=["echo"])
    user = _user()

    chunks = list(proxy.stream({"input": "hi"}, user=user))

    assert chunks == [{"chunk": 1}, {"chunk": 2}]
    [call] = graph.stream_calls
    assert call["user"] is user
    assert proxy._callback_handler in call["config"]["callbacks"]
    assert get_current_user() is None


@pytest.mark.asyncio
async def test_astream_opens_user_scope_and_yields_chunks() -> None:
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(agent=graph, api_key="k", tool_names=["echo"])
    user = _user()

    chunks = [chunk async for chunk in proxy.astream({"input": "hi"}, user=user)]

    assert chunks == [{"chunk": 1}, {"chunk": 2}]
    [call] = graph.astream_calls
    assert call["user"] is user
    assert get_current_user() is None


@pytest.mark.asyncio
async def test_astream_events_forwards_version_and_opens_scope() -> None:
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(agent=graph, api_key="k", tool_names=["echo"])
    user = _user()

    events = [
        evt async for evt in proxy.astream_events({"input": "hi"}, "v2", user=user)
    ]

    assert events == [{"event": "start"}, {"event": "end"}]
    [call] = graph.astream_events_calls
    assert call["version"] == "v2"
    assert call["user"] is user
    assert get_current_user() is None


def test_user_scope_is_unwound_when_invoke_raises() -> None:
    """The contextvar unwinds even when the wrapped agent raises."""

    class BoomGraph:
        name = "boom"

        def invoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("boom")

    proxy = FortifyLangchainAgent(agent=BoomGraph(), api_key="k", tool_names=[])

    with pytest.raises(RuntimeError, match="boom"):
        proxy.invoke({"input": "hi"}, user=_user())

    assert get_current_user() is None


# ---------------------------------------------------------------------------
# __getattr__ delegation
# ---------------------------------------------------------------------------


def test_proxy_delegates_unknown_attributes_to_wrapped_agent() -> None:
    graph = _RecordingGraph()
    proxy = FortifyLangchainAgent(agent=graph, api_key="k", tool_names=[])

    assert proxy.some_attribute() == "delegated"
    assert proxy.name == "recording-graph"
