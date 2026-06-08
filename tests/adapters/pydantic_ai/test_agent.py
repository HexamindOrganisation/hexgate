"""Tests for the FortifyPydanticAgent proxy."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import pytest

from fortify.adapters.pydantic_ai.agent import FortifyPydanticAgent
from fortify.runtime import User
from fortify.runtime.context import get_current_user


def _user() -> User:
    """Build a minimal User for invocation tests."""
    return User(user_id="u-1", session_id="s-1", role="developer")


class _RecordingAgent:
    """Capture the active User and call args seen by each Agent method."""

    name = "recording-agent"

    def __init__(self) -> None:
        self.run_calls: list[dict[str, Any]] = []
        self.run_sync_calls: list[dict[str, Any]] = []
        self.run_stream_calls: list[dict[str, Any]] = []
        self.iter_calls: list[dict[str, Any]] = []

    def _snapshot(
        self, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        """Capture the active User plus call arguments."""
        return {
            "user": get_current_user(),
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
        """Async-context yield while capturing the active User."""
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


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


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
    )

    assert calls == [True]


def test_constructor_stores_inputs() -> None:
    """The proxy keeps the agent, api key, agent name, and tool names verbatim."""
    inner = _RecordingAgent()

    proxy = FortifyPydanticAgent(
        agent=inner,  # type: ignore[arg-type]
        api_key="api-123",
        agent_name="custom-name",
    )

    assert proxy._agent is inner
    assert proxy._api_key == "api-123"
    assert proxy._agent_name == "custom-name"


# ---------------------------------------------------------------------------
# User scope binding per invocation method
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_opens_user_scope_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "fortify.adapters.pydantic_ai.agent.Agent.instrument_all", lambda: None
    )

    inner = _RecordingAgent()
    proxy = FortifyPydanticAgent(
        agent=inner,  # type: ignore[arg-type]
        api_key="k",
        agent_name="recording-agent",
    )
    user = _user()

    assert get_current_user() is None

    result = await proxy.run("hello", user=user)

    assert result == "run-ok"
    [call] = inner.run_calls
    assert call["user"] is user
    assert call["args"] == ("hello",)
    assert get_current_user() is None


def test_run_sync_opens_user_scope_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "fortify.adapters.pydantic_ai.agent.Agent.instrument_all", lambda: None
    )

    inner = _RecordingAgent()
    proxy = FortifyPydanticAgent(
        agent=inner,  # type: ignore[arg-type]
        api_key="k",
        agent_name="recording-agent",
    )
    user = _user()

    result = proxy.run_sync("hello", user=user)

    assert result == "run-sync-ok"
    [call] = inner.run_sync_calls
    assert call["user"] is user
    assert get_current_user() is None


@pytest.mark.asyncio
async def test_run_stream_opens_user_scope_and_yields_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "fortify.adapters.pydantic_ai.agent.Agent.instrument_all", lambda: None
    )

    inner = _RecordingAgent()
    proxy = FortifyPydanticAgent(
        agent=inner,  # type: ignore[arg-type]
        api_key="k",
        agent_name="recording-agent",
    )
    user = _user()

    async with proxy.run_stream("hello", user=user) as result:
        assert result == "stream-result"
        # Scope is live during the body.
        assert get_current_user() is user

    [call] = inner.run_stream_calls
    assert call["user"] is user
    assert get_current_user() is None


@pytest.mark.asyncio
async def test_iter_opens_user_scope_and_yields_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "fortify.adapters.pydantic_ai.agent.Agent.instrument_all", lambda: None
    )

    inner = _RecordingAgent()
    proxy = FortifyPydanticAgent(
        agent=inner,  # type: ignore[arg-type]
        api_key="k",
        agent_name="recording-agent",
    )
    user = _user()

    async with proxy.iter("hello", user=user) as run:
        assert run == "iter-result"
        assert get_current_user() is user

    [call] = inner.iter_calls
    assert call["user"] is user
    assert get_current_user() is None


def test_user_scope_is_unwound_when_run_sync_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contextvar unwinds even when the wrapped agent raises."""
    monkeypatch.setattr(
        "fortify.adapters.pydantic_ai.agent.Agent.instrument_all", lambda: None
    )

    class BoomAgent:
        def run_sync(self, *_args: Any, **_kwargs: Any) -> str:
            raise RuntimeError("boom")

    proxy = FortifyPydanticAgent(
        agent=BoomAgent(),  # type: ignore[arg-type]
        api_key="k",
        agent_name="boom",
    )

    with pytest.raises(RuntimeError, match="boom"):
        proxy.run_sync("hi", user=_user())

    assert get_current_user() is None


# ---------------------------------------------------------------------------
# __getattr__ delegation
# ---------------------------------------------------------------------------


def test_proxy_delegates_unknown_attributes_to_wrapped_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "fortify.adapters.pydantic_ai.agent.Agent.instrument_all", lambda: None
    )

    inner = _RecordingAgent()
    proxy = FortifyPydanticAgent(
        agent=inner,  # type: ignore[arg-type]
        api_key="k",
        agent_name="recording-agent",
    )

    assert proxy.some_attribute() == "delegated"
    assert proxy.name == "recording-agent"


# ---------------------------------------------------------------------------
# Per-run policy refresh (phase 7)
# ---------------------------------------------------------------------------


class _CountingBinding:
    def __init__(self) -> None:
        self.refreshes = 0

    def refresh(self) -> None:
        self.refreshes += 1

    async def refresh_async(self) -> None:
        self.refreshes += 1


def _proxy_with_counting_binding() -> tuple[FortifyPydanticAgent, _CountingBinding]:
    binding = _CountingBinding()
    proxy = FortifyPydanticAgent(
        agent=_RecordingAgent(),  # type: ignore[arg-type]
        api_key="k",
        agent_name="recording-agent",
        binding=binding,  # type: ignore[arg-type]
    )
    return proxy, binding


@pytest.mark.asyncio
async def test_run_refreshes_binding_per_call() -> None:
    proxy, binding = _proxy_with_counting_binding()

    await proxy.run("one", user=_user())
    await proxy.run("two", user=_user())

    assert binding.refreshes == 2


def test_run_sync_refreshes_binding_per_call() -> None:
    proxy, binding = _proxy_with_counting_binding()

    proxy.run_sync("one", user=_user())

    assert binding.refreshes == 1


@pytest.mark.asyncio
async def test_run_stream_refreshes_binding_per_call() -> None:
    proxy, binding = _proxy_with_counting_binding()

    async with proxy.run_stream("one", user=_user()) as result:
        assert result == "stream-result"

    assert binding.refreshes == 1


@pytest.mark.asyncio
async def test_iter_refreshes_binding_per_call() -> None:
    proxy, binding = _proxy_with_counting_binding()

    async with proxy.iter("one", user=_user()):
        pass

    assert binding.refreshes == 1


def test_proxy_without_binding_runs_fine() -> None:
    """Back-compat: a binding-less proxy (direct construction) still works."""
    proxy = FortifyPydanticAgent(
        agent=_RecordingAgent(),  # type: ignore[arg-type]
        api_key="k",
        agent_name="recording-agent",
    )

    assert proxy.run_sync("one", user=_user()) == "run-sync-ok"
