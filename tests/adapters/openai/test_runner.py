"""Tests for the FortifyRunner that wraps the OpenAI Agents Runner."""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest
from agents import Agent, FunctionTool

from fortify.adapters.openai.runner import FortifyRunner
from fortify.user_context import UserContext


def _user_context() -> UserContext:
    """Build a minimal UserContext for runner tests."""
    return UserContext(user_id="u-1", session_id="s-1", user_role="developer")


def _make_tool(name: str = "echo") -> FunctionTool:
    """Build a minimal FunctionTool for runner tests."""

    async def on_invoke(_ctx: Any, raw_args: str) -> str:
        return f"invoked:{raw_args}"

    return FunctionTool(
        name=name,
        description=f"{name} tool",
        params_json_schema={"type": "object"},
        on_invoke_tool=on_invoke,
    )


def _make_agent(name: str = "my-agent") -> Agent:
    """Build a minimal Agent fixture for runner tests."""
    return Agent(name=name, tools=[_make_tool("echo")])


def _silence_observability(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Replace heavy observability dependencies with no-op stubs."""
    counts = {"setup": 0, "instrument": 0, "get_client": 0}

    def fake_setup(self: Any) -> None:
        counts["setup"] += 1

    monkeypatch.setattr(FortifyRunner, "_setup_observability", fake_setup)
    return counts


class _FakeStreamingResult:
    """Stand in for a RunResultStreaming with a swappable stream_events callable."""

    def __init__(self) -> None:
        """Initialize with a baseline stream_events that yields two events."""

        async def baseline() -> AsyncIterator[dict[str, str]]:
            yield {"event": "first"}
            yield {"event": "second"}

        self.stream_events = baseline


def test_constructor_uses_explicit_api_key() -> None:
    """An explicit api_key argument is stored verbatim."""
    runner = FortifyRunner(api_key="explicit-key")

    assert runner.api_key == "explicit-key"


def test_constructor_falls_back_to_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the API key from FORTIFY_KEY when no explicit key is given."""
    monkeypatch.setenv("FORTIFY_KEY", "from-env")

    runner = FortifyRunner()

    assert runner.api_key == "from-env"


def test_constructor_prefers_explicit_api_key_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit api_key argument wins when both sources are populated."""
    monkeypatch.setenv("FORTIFY_KEY", "from-env")

    runner = FortifyRunner(api_key="explicit")

    assert runner.api_key == "explicit"


def test_constructor_raises_when_no_api_key_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject construction when neither argument nor env var supplies a key."""
    monkeypatch.delenv("FORTIFY_KEY", raising=False)

    with pytest.raises(ValueError, match="FORTIFY_KEY is not set"):
        FortifyRunner()


@pytest.mark.asyncio
async def test_run_wraps_agent_and_calls_runner_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run() wraps the agent with policy gates and forwards to Runner.run."""
    setup_counts = _silence_observability(monkeypatch)
    captured: dict[str, Any] = {}

    async def fake_run(starting_agent: Agent, input: Any, **kwargs: Any) -> str:
        captured["agent"] = starting_agent
        captured["input"] = input
        captured["kwargs"] = kwargs
        return "run-result"

    monkeypatch.setattr(
        "fortify.adapters.openai.runner.Runner.run", staticmethod(fake_run)
    )

    runner = FortifyRunner(api_key="k")
    agent = _make_agent()

    result = await runner.run(agent, "hello", user_context=_user_context())

    assert result == "run-result"
    assert setup_counts["setup"] == 1
    assert captured["agent"] is not agent
    assert captured["agent"].name == agent.name
    assert captured["input"] == "hello"
    assert captured["kwargs"] == {"run_config": None}


def test_run_sync_wraps_agent_and_calls_runner_run_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_sync() wraps the agent and forwards to Runner.run_sync."""
    setup_counts = _silence_observability(monkeypatch)
    captured: dict[str, Any] = {}

    def fake_run_sync(starting_agent: Agent, input: Any, **kwargs: Any) -> str:
        captured["agent"] = starting_agent
        captured["input"] = input
        captured["kwargs"] = kwargs
        return "run-sync-result"

    monkeypatch.setattr(
        "fortify.adapters.openai.runner.Runner.run_sync", staticmethod(fake_run_sync)
    )

    runner = FortifyRunner(api_key="k")
    agent = _make_agent()

    result = runner.run_sync(agent, "hello", user_context=_user_context())

    assert result == "run-sync-result"
    assert setup_counts["setup"] == 1
    assert captured["agent"] is not agent
    assert captured["input"] == "hello"


@pytest.mark.asyncio
async def test_run_streamed_wraps_stream_events_to_re_enter_propagation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_streamed swaps stream_events for a wrapper that re-enters propagation."""
    _silence_observability(monkeypatch)

    fake_result = _FakeStreamingResult()
    captured: dict[str, Any] = {}

    def fake_run_streamed(
        starting_agent: Agent, input: Any, **kwargs: Any
    ) -> _FakeStreamingResult:
        captured["agent"] = starting_agent
        captured["input"] = input
        captured["kwargs"] = kwargs
        return fake_result

    monkeypatch.setattr(
        "fortify.adapters.openai.runner.Runner.run_streamed",
        staticmethod(fake_run_streamed),
    )

    propagate_calls: list[dict[str, Any]] = []

    from contextlib import contextmanager

    @contextmanager
    def fake_propagate_attributes(**kwargs: Any) -> Any:
        propagate_calls.append(kwargs)
        yield

    monkeypatch.setattr(
        "fortify.adapters.openai.runner.propagate_attributes",
        fake_propagate_attributes,
    )

    runner = FortifyRunner(api_key="k")
    agent = _make_agent()

    result = runner.run_streamed(agent, "hello", user_context=_user_context())

    assert result is fake_result
    assert captured["agent"].name == agent.name
    assert len(propagate_calls) == 1
    assert propagate_calls[0]["user_id"] == "u-1"
    assert propagate_calls[0]["session_id"] == "s-1"
    assert propagate_calls[0]["metadata"] == {"user_role": "developer"}
    assert propagate_calls[0]["tags"] == ["openai.runner.run.my-agent"]

    events = [event async for event in result.stream_events()]
    assert events == [{"event": "first"}, {"event": "second"}]
    assert len(propagate_calls) == 2


@pytest.mark.asyncio
async def test_run_propagates_user_context_to_langfuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run() enters propagate_attributes with user identity and an agent-tagged scope."""
    _silence_observability(monkeypatch)

    async def fake_run(*_args: Any, **_kwargs: Any) -> str:
        return "ok"

    monkeypatch.setattr(
        "fortify.adapters.openai.runner.Runner.run", staticmethod(fake_run)
    )

    propagate_calls: list[dict[str, Any]] = []

    from contextlib import contextmanager

    @contextmanager
    def fake_propagate_attributes(**kwargs: Any) -> Any:
        propagate_calls.append(kwargs)
        yield

    monkeypatch.setattr(
        "fortify.adapters.openai.runner.propagate_attributes",
        fake_propagate_attributes,
    )

    runner = FortifyRunner(api_key="k")

    await runner.run(_make_agent("custom-name"), "hi", user_context=_user_context())

    [call] = propagate_calls
    assert call["tags"] == ["openai.runner.run.custom-name"]
    assert call["user_id"] == "u-1"
    assert call["session_id"] == "s-1"
    assert call["metadata"] == {"user_role": "developer"}
