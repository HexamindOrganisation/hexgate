"""Tests for the FortifyPydanticAgent proxy."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import pytest

from fortify.adapters.pydantic_ai import tools as pa_tools
from fortify.adapters.pydantic_ai.agent import FortifyPydanticAgent
from fortify.security import AgentPolicy
from fortify.runtime import UserContext


def _user_context() -> UserContext:
    """Build a minimal UserContext for invocation tests."""
    return UserContext(user_id="u-1", session_id="s-1", user_role="developer")


class _RecordingAgent:
    """Capture the active policy and call args seen by each Agent method."""

    name = "recording-agent"

    def __init__(self) -> None:
        """Initialize empty capture slots."""
        self.run_calls: list[dict[str, Any]] = []
        self.run_sync_calls: list[dict[str, Any]] = []
        self.run_stream_calls: list[dict[str, Any]] = []
        self.iter_calls: list[dict[str, Any]] = []

    def _snapshot(
        self, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        """Capture the active policy plus call arguments."""
        return {
            "policy": pa_tools._active_policy.get(),
            "args": args,
            "kwargs": kwargs,
        }

    async def run(self, *args: Any, **kwargs: Any) -> str:
        """Record async-run arguments."""
        self.run_calls.append(self._snapshot(args, kwargs))
        return "run-ok"

    def run_sync(self, *args: Any, **kwargs: Any) -> str:
        """Record sync-run arguments."""
        self.run_sync_calls.append(self._snapshot(args, kwargs))
        return "run-sync-ok"

    @asynccontextmanager
    async def run_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        """Async-context yield while capturing the active policy."""
        self.run_stream_calls.append(self._snapshot(args, kwargs))
        yield "stream-result"

    @asynccontextmanager
    async def iter(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        """Async-context yield used by graph iteration."""
        self.iter_calls.append(self._snapshot(args, kwargs))
        yield "iter-result"

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
        "fortify.adapters.pydantic_ai.agent.build_agent_policy", fake_build
    )
    return captured


def test_constructor_calls_setup_observability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy must instrument all pydantic_ai Agents at construction."""
    calls: list[bool] = []

    def fake_instrument_all() -> None:
        calls.append(True)

    monkeypatch.setattr(
        "fortify.adapters.pydantic_ai.agent.Agent.instrument_all", fake_instrument_all
    )

    FortifyPydanticAgent(
        agent=_RecordingAgent(),  # type: ignore[arg-type]
        api_key="k",
        agent_name="recording-agent",
        tool_names=[],
    )

    assert calls == [True]


def test_constructor_stores_inputs() -> None:
    """The proxy keeps the agent, api key, agent name, and tool names verbatim."""
    inner = _RecordingAgent()

    proxy = FortifyPydanticAgent(
        agent=inner,  # type: ignore[arg-type]
        api_key="api-123",
        agent_name="custom-name",
        tool_names=["a", "b"],
    )

    assert proxy._agent is inner
    assert proxy._api_key == "api-123"
    assert proxy._agent_name == "custom-name"
    assert proxy._tool_names == ["a", "b"]


@pytest.mark.asyncio
async def test_run_binds_active_policy_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind the policy for the call and forward to the underlying agent."""
    captured = _stub_build_agent_policy(monkeypatch)
    inner = _RecordingAgent()
    proxy = FortifyPydanticAgent(
        agent=inner,  # type: ignore[arg-type]
        api_key="api-key-123",
        agent_name="recording-agent",
        tool_names=["echo"],
    )

    assert pa_tools._active_policy.get() is None

    result = await proxy.run("hello", user_context=_user_context())

    assert result == "run-ok"
    [call] = inner.run_calls
    assert call["policy"] is captured["calls"][0]["returned_policy"]
    assert call["args"] == ("hello",)
    assert pa_tools._active_policy.get() is None


@pytest.mark.asyncio
async def test_run_forwards_user_context_into_policy_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass api_key, user_context, agent name, and tool names to build_agent_policy."""
    captured = _stub_build_agent_policy(monkeypatch)
    inner = _RecordingAgent()
    proxy = FortifyPydanticAgent(
        agent=inner,  # type: ignore[arg-type]
        api_key="api-key-123",
        agent_name="recording-agent",
        tool_names=["echo", "search"],
    )
    ctx = _user_context()

    await proxy.run("hello", user_context=ctx)

    [policy_call] = captured["calls"]
    assert policy_call["api_key"] == "api-key-123"
    assert policy_call["context"] is ctx
    assert policy_call["agent_name"] == "recording-agent"
    assert policy_call["tool_names"] == ["echo", "search"]


def test_run_sync_binds_active_policy_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync-run binds and delegates the same way as async run."""
    captured = _stub_build_agent_policy(monkeypatch)
    inner = _RecordingAgent()
    proxy = FortifyPydanticAgent(
        agent=inner,  # type: ignore[arg-type]
        api_key="k",
        agent_name="recording-agent",
        tool_names=["echo"],
    )

    result = proxy.run_sync("hello", user_context=_user_context())

    assert result == "run-sync-ok"
    [call] = inner.run_sync_calls
    assert call["policy"] is captured["calls"][0]["returned_policy"]
    assert call["args"] == ("hello",)
    assert pa_tools._active_policy.get() is None


@pytest.mark.asyncio
async def test_run_stream_binds_active_policy_and_yields_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_stream binds the policy for the lifetime of the streamed result."""
    captured = _stub_build_agent_policy(monkeypatch)
    inner = _RecordingAgent()
    proxy = FortifyPydanticAgent(
        agent=inner,  # type: ignore[arg-type]
        api_key="k",
        agent_name="recording-agent",
        tool_names=["echo"],
    )

    async with proxy.run_stream("hello", user_context=_user_context()) as result:
        assert result == "stream-result"
        assert pa_tools._active_policy.get() is captured["calls"][0]["returned_policy"]

    assert pa_tools._active_policy.get() is None
    [call] = inner.run_stream_calls
    assert call["args"] == ("hello",)


@pytest.mark.asyncio
async def test_iter_binds_active_policy_and_yields_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """iter binds the policy for the lifetime of the async iterator."""
    captured = _stub_build_agent_policy(monkeypatch)
    inner = _RecordingAgent()
    proxy = FortifyPydanticAgent(
        agent=inner,  # type: ignore[arg-type]
        api_key="k",
        agent_name="recording-agent",
        tool_names=["echo"],
    )

    async with proxy.iter("hello", user_context=_user_context()) as agent_run:
        assert agent_run == "iter-result"
        assert pa_tools._active_policy.get() is captured["calls"][0]["returned_policy"]

    assert pa_tools._active_policy.get() is None
    [call] = inner.iter_calls
    assert call["args"] == ("hello",)


def test_active_policy_is_unset_when_run_sync_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tear down the active policy even when the wrapped agent raises."""
    _stub_build_agent_policy(monkeypatch)

    class BoomAgent:
        name = "boom"

        def run_sync(self, *_args: Any, **_kwargs: Any) -> str:
            raise RuntimeError("boom")

    proxy = FortifyPydanticAgent(
        agent=BoomAgent(),  # type: ignore[arg-type]
        api_key="k",
        agent_name="boom",
        tool_names=[],
    )

    with pytest.raises(RuntimeError, match="boom"):
        proxy.run_sync("hi", user_context=_user_context())

    assert pa_tools._active_policy.get() is None


@pytest.mark.asyncio
async def test_active_policy_is_unset_when_run_stream_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tear down the active policy when the streamed body raises inside the context."""
    _stub_build_agent_policy(monkeypatch)
    inner = _RecordingAgent()
    proxy = FortifyPydanticAgent(
        agent=inner,  # type: ignore[arg-type]
        api_key="k",
        agent_name="recording-agent",
        tool_names=[],
    )

    with pytest.raises(RuntimeError, match="boom"):
        async with proxy.run_stream("hi", user_context=_user_context()):
            raise RuntimeError("boom")

    assert pa_tools._active_policy.get() is None


def test_proxy_delegates_unknown_attributes_to_wrapped_agent() -> None:
    """__getattr__ falls through to the wrapped pydantic_ai Agent."""
    inner = _RecordingAgent()
    proxy = FortifyPydanticAgent(
        agent=inner,  # type: ignore[arg-type]
        api_key="k",
        agent_name="recording-agent",
        tool_names=[],
    )

    assert proxy.some_attribute() == "delegated"
    assert proxy.name == "recording-agent"
