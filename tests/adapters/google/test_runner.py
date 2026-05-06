"""Tests for the FortifyRunner that wraps the Google ADK Runner."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, AsyncIterator

import pytest
from google.adk.agents import LlmAgent
from google.adk.sessions import InMemorySessionService
from google.adk.tools.function_tool import FunctionTool

from fortify.adapters.google.runner import FortifyRunner
from fortify.runtime import UserContext


def _user_context() -> UserContext:
    """Build a minimal UserContext for runner tests."""
    return UserContext(user_id="u-1", session_id="s-1", user_role="developer")


def _make_callable(name: str = "echo") -> Any:
    """Build a plain callable echo function."""

    def echo(text: str) -> str:
        """Echo the input back."""
        return f"echo:{text}"

    echo.__name__ = name
    return echo


def _make_agent(name: str = "my_agent") -> LlmAgent:
    """Build a minimal ADK agent fixture for runner tests."""
    return LlmAgent(
        name=name,
        model="gemini-2.0-flash",
        tools=[FunctionTool(func=_make_callable("echo"))],
    )


def _silence_observability(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Replace heavy observability dependencies with no-op stubs."""
    counts = {"setup": 0}

    def fake_setup(self: Any) -> None:
        counts["setup"] += 1

    monkeypatch.setattr(FortifyRunner, "_setup_observability", fake_setup)
    return counts


class _FakeRunner:
    """Capture the construction args and yield events for run / run_async."""

    instances: list["_FakeRunner"] = []

    def __init__(self, **kwargs: Any) -> None:
        """Record construction kwargs and reset call captures."""
        self.kwargs = kwargs
        self.run_calls: list[dict[str, Any]] = []
        self.run_async_calls: list[dict[str, Any]] = []
        _FakeRunner.instances.append(self)

    def run(self, **kwargs: Any) -> Any:
        """Yield two synthetic events while capturing the call kwargs."""
        self.run_calls.append(kwargs)
        yield {"event": "first"}
        yield {"event": "second"}

    async def run_async(self, **kwargs: Any) -> AsyncIterator[dict[str, str]]:
        """Async-yield two synthetic events while capturing the call kwargs."""
        self.run_async_calls.append(kwargs)
        yield {"event": "first"}
        yield {"event": "second"}


def _install_fake_runner(monkeypatch: pytest.MonkeyPatch) -> type[_FakeRunner]:
    """Patch the runner module's Runner symbol with the recording fake."""
    _FakeRunner.instances = []
    monkeypatch.setattr("fortify.adapters.google.runner.Runner", _FakeRunner)
    return _FakeRunner


def test_constructor_uses_explicit_api_key() -> None:
    """An explicit api_key argument is stored verbatim."""
    runner = FortifyRunner(
        agent=_make_agent(),
        app_name="app",
        session_service=InMemorySessionService(),
        api_key="explicit-key",
    )

    assert runner.api_key == "explicit-key"


def test_constructor_falls_back_to_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the API key from FORTIFY_KEY when no explicit key is given."""
    monkeypatch.setenv("FORTIFY_KEY", "from-env")

    runner = FortifyRunner(
        agent=_make_agent(),
        app_name="app",
        session_service=InMemorySessionService(),
    )

    assert runner.api_key == "from-env"


def test_constructor_prefers_explicit_api_key_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit api_key argument wins when both sources are populated."""
    monkeypatch.setenv("FORTIFY_KEY", "from-env")

    runner = FortifyRunner(
        agent=_make_agent(),
        app_name="app",
        session_service=InMemorySessionService(),
        api_key="explicit",
    )

    assert runner.api_key == "explicit"


def test_constructor_raises_when_no_api_key_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject construction when neither argument nor env var supplies a key."""
    monkeypatch.delenv("FORTIFY_KEY", raising=False)

    with pytest.raises(ValueError, match="FORTIFY_KEY is not set"):
        FortifyRunner(
            agent=_make_agent(),
            app_name="app",
            session_service=InMemorySessionService(),
        )


def test_constructor_stores_extra_runner_kwargs() -> None:
    """Extra kwargs are stashed for the eventual Runner construction."""
    runner = FortifyRunner(
        agent=_make_agent(),
        app_name="app",
        session_service=InMemorySessionService(),
        api_key="k",
        custom_kwarg="value",
    )

    assert runner._runner_kwargs == {"custom_kwarg": "value"}


def test_run_yields_events_and_constructs_runner_with_wrapped_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run() builds a Runner with a wrapped agent and yields events from it."""
    setup_counts = _silence_observability(monkeypatch)
    fake = _install_fake_runner(monkeypatch)

    session_service = InMemorySessionService()
    agent = _make_agent()
    runner = FortifyRunner(
        agent=agent,
        app_name="my-app",
        session_service=session_service,
        api_key="k",
    )

    events = list(runner.run(new_message="hello", user_context=_user_context()))

    assert events == [{"event": "first"}, {"event": "second"}]
    assert setup_counts["setup"] == 1
    [fake_runner] = fake.instances
    assert fake_runner.kwargs["app_name"] == "my-app"
    assert fake_runner.kwargs["session_service"] is session_service
    wrapped_agent = fake_runner.kwargs["agent"]
    assert wrapped_agent is not agent
    assert wrapped_agent.name == agent.name
    [run_call] = fake_runner.run_calls
    assert run_call == {
        "user_id": "u-1",
        "session_id": "s-1",
        "new_message": "hello",
    }


@pytest.mark.asyncio
async def test_run_async_yields_events_and_constructs_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_async() builds a Runner with a wrapped agent and yields events."""
    setup_counts = _silence_observability(monkeypatch)
    fake = _install_fake_runner(monkeypatch)

    runner = FortifyRunner(
        agent=_make_agent(),
        app_name="my-app",
        session_service=InMemorySessionService(),
        api_key="k",
    )

    events = [
        event
        async for event in runner.run_async(
            new_message="hello", user_context=_user_context()
        )
    ]

    assert events == [{"event": "first"}, {"event": "second"}]
    assert setup_counts["setup"] == 1
    [fake_runner] = fake.instances
    [run_call] = fake_runner.run_async_calls
    assert run_call == {
        "user_id": "u-1",
        "session_id": "s-1",
        "new_message": "hello",
    }


def test_run_propagates_user_context_to_langfuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run() enters propagate_attributes with user identity and an agent-tagged scope."""
    _silence_observability(monkeypatch)
    _install_fake_runner(monkeypatch)

    propagate_calls: list[dict[str, Any]] = []

    @contextmanager
    def fake_propagate_attributes(**kwargs: Any) -> Any:
        propagate_calls.append(kwargs)
        yield

    monkeypatch.setattr(
        "fortify.adapters.google.runner.propagate_attributes",
        fake_propagate_attributes,
    )

    runner = FortifyRunner(
        agent=_make_agent("custom_agent"),
        app_name="app",
        session_service=InMemorySessionService(),
        api_key="k",
    )

    list(runner.run(new_message="hi", user_context=_user_context()))

    [call] = propagate_calls
    assert call["tags"] == ["google.runner.run.custom_agent"]
    assert call["user_id"] == "u-1"
    assert call["session_id"] == "s-1"
    assert call["metadata"] == {"user_role": "developer"}


@pytest.mark.asyncio
async def test_run_async_propagates_user_context_to_langfuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_async() also propagates the user context for each invocation."""
    _silence_observability(monkeypatch)
    _install_fake_runner(monkeypatch)

    propagate_calls: list[dict[str, Any]] = []

    @contextmanager
    def fake_propagate_attributes(**kwargs: Any) -> Any:
        propagate_calls.append(kwargs)
        yield

    monkeypatch.setattr(
        "fortify.adapters.google.runner.propagate_attributes",
        fake_propagate_attributes,
    )

    runner = FortifyRunner(
        agent=_make_agent("custom_agent"),
        app_name="app",
        session_service=InMemorySessionService(),
        api_key="k",
    )

    async for _ in runner.run_async(new_message="hi", user_context=_user_context()):
        pass

    [call] = propagate_calls
    assert call["tags"] == ["google.runner.run.custom_agent"]
    assert call["user_id"] == "u-1"
    assert call["session_id"] == "s-1"


def test_build_runner_passes_extra_kwargs_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extra kwargs given to the constructor reach the underlying Runner."""
    _silence_observability(monkeypatch)
    fake = _install_fake_runner(monkeypatch)

    runner = FortifyRunner(
        agent=_make_agent(),
        app_name="app",
        session_service=InMemorySessionService(),
        api_key="k",
        custom_kwarg="value",
    )

    list(runner.run(new_message="hi", user_context=_user_context()))

    [fake_runner] = fake.instances
    assert fake_runner.kwargs["custom_kwarg"] == "value"
