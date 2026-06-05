"""Tests for the FortifyRunner that wraps the Google ADK Runner."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, AsyncIterator

import pytest
from google.adk.agents import LlmAgent
from google.adk.sessions import InMemorySessionService
from google.adk.tools.function_tool import FunctionTool

from fortify.adapters.google import wrapper as wrapper_mod
from fortify.adapters.google.runner import FortifyRunner
from fortify.runtime import User
from fortify.runtime.context import get_current_user
from fortify.security import AgentPolicy, BaseToolPolicy, PolicyBinding, PolicySet
from fortify.security.enforcer import PolicyEnforcer
from fortify.security.policy_set import DEFAULT_ROLE_NAME


@pytest.fixture(autouse=True)
def _stub_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the platform resolve seam — runner tests are about lifecycle,
    not policy resolution (covered by test_wrapper.py / binding tests)."""

    def fake_resolve(agent: Any, name: str, key: str) -> PolicyBinding:
        tool_names = [
            getattr(t, "name", getattr(t, "__name__", "tool"))
            for t in (getattr(agent, "tools", []) or [])
        ]
        engine = PolicySet(
            {
                DEFAULT_ROLE_NAME: AgentPolicy(
                    tools={n: BaseToolPolicy(mode="allow") for n in tool_names}
                )
            }
        )
        return PolicyBinding(PolicyEnforcer(engine, agent_name=name))

    monkeypatch.setattr(wrapper_mod, "_resolve_binding", fake_resolve)


def _user() -> User:
    """Build a minimal User for runner tests."""
    return User(user_id="u-1", session_id="s-1", role="developer")


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
        self.kwargs = kwargs
        self.run_calls: list[dict[str, Any]] = []
        self.run_async_calls: list[dict[str, Any]] = []
        # Capture which User was active at each call (verifies the scope is live).
        self.active_users: list[Any] = []
        _FakeRunner.instances.append(self)

    def run(self, **kwargs: Any) -> Any:
        """Yield two synthetic events while capturing the call kwargs."""
        self.run_calls.append(kwargs)
        self.active_users.append(get_current_user())
        yield {"event": "first"}
        yield {"event": "second"}

    async def run_async(self, **kwargs: Any) -> AsyncIterator[dict[str, str]]:
        """Async-yield two synthetic events while capturing the call kwargs."""
        self.run_async_calls.append(kwargs)
        self.active_users.append(get_current_user())
        yield {"event": "first"}
        yield {"event": "second"}


def _install_fake_runner(monkeypatch: pytest.MonkeyPatch) -> type[_FakeRunner]:
    """Patch the runner module's Runner symbol with the recording fake."""
    _FakeRunner.instances = []
    monkeypatch.setattr("fortify.adapters.google.runner.Runner", _FakeRunner)
    return _FakeRunner


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_constructor_uses_explicit_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit api_key argument is stored verbatim."""
    _install_fake_runner(monkeypatch)

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
    _install_fake_runner(monkeypatch)

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
    _install_fake_runner(monkeypatch)

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


def test_constructor_builds_underlying_runner_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construction wraps the agent + builds the Runner exactly once."""
    fake = _install_fake_runner(monkeypatch)

    FortifyRunner(
        agent=_make_agent(),
        app_name="app",
        session_service=InMemorySessionService(),
        api_key="k",
        custom_kwarg="value",
    )

    [fake_runner] = fake.instances
    assert fake_runner.kwargs["app_name"] == "app"
    assert fake_runner.kwargs["custom_kwarg"] == "value"
    # The wrapped agent is a clone, not the original.
    assert fake_runner.kwargs["agent"].name == "my_agent"


# ---------------------------------------------------------------------------
# run / run_async — User scope + delegation
# ---------------------------------------------------------------------------


def test_run_opens_user_scope_and_yields_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run() opens a User scope around the underlying Runner.run."""
    setup_counts = _silence_observability(monkeypatch)
    fake = _install_fake_runner(monkeypatch)

    runner = FortifyRunner(
        agent=_make_agent(),
        app_name="my-app",
        session_service=InMemorySessionService(),
        api_key="k",
    )
    user = _user()

    events = list(runner.run(new_message="hello", user=user))

    assert events == [{"event": "first"}, {"event": "second"}]
    assert setup_counts["setup"] == 1
    [fake_runner] = fake.instances
    [run_call] = fake_runner.run_calls
    assert run_call == {
        "user_id": "u-1",
        "session_id": "s-1",
        "new_message": "hello",
    }
    # User scope was live during the underlying call.
    [active] = fake_runner.active_users
    assert active is user
    # Scope unwound after the call.
    assert get_current_user() is None


@pytest.mark.asyncio
async def test_run_async_opens_user_scope_and_yields_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_async() opens a User scope around the underlying Runner.run_async."""
    setup_counts = _silence_observability(monkeypatch)
    fake = _install_fake_runner(monkeypatch)

    runner = FortifyRunner(
        agent=_make_agent(),
        app_name="my-app",
        session_service=InMemorySessionService(),
        api_key="k",
    )
    user = _user()

    events = [event async for event in runner.run_async(new_message="hello", user=user)]

    assert events == [{"event": "first"}, {"event": "second"}]
    assert setup_counts["setup"] == 1
    [fake_runner] = fake.instances
    [run_call] = fake_runner.run_async_calls
    assert run_call == {
        "user_id": "u-1",
        "session_id": "s-1",
        "new_message": "hello",
    }
    [active] = fake_runner.active_users
    assert active is user
    assert get_current_user() is None


# ---------------------------------------------------------------------------
# Langfuse propagation
# ---------------------------------------------------------------------------


def test_run_propagates_user_identity_to_langfuse(
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

    list(runner.run(new_message="hi", user=_user()))

    [call] = propagate_calls
    assert call["tags"] == ["google.runner.run.custom_agent"]
    assert call["user_id"] == "u-1"
    assert call["session_id"] == "s-1"
    assert call["metadata"] == {"user_role": "developer"}


@pytest.mark.asyncio
async def test_run_async_propagates_user_identity_to_langfuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_async() also propagates the user identity for each invocation."""
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

    async for _ in runner.run_async(new_message="hi", user=_user()):
        pass

    [call] = propagate_calls
    assert call["tags"] == ["google.runner.run.custom_agent"]
    assert call["user_id"] == "u-1"
    assert call["session_id"] == "s-1"


# ---------------------------------------------------------------------------
# Extra kwargs threading
# ---------------------------------------------------------------------------


def test_extra_kwargs_reach_underlying_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extra kwargs given to the constructor reach the underlying Runner."""
    _silence_observability(monkeypatch)
    fake = _install_fake_runner(monkeypatch)

    FortifyRunner(
        agent=_make_agent(),
        app_name="app",
        session_service=InMemorySessionService(),
        api_key="k",
        custom_kwarg="value",
    )

    [fake_runner] = fake.instances
    assert fake_runner.kwargs["custom_kwarg"] == "value"


# ---------------------------------------------------------------------------
# Per-run policy refresh (phase 5)
# ---------------------------------------------------------------------------


class _CountingBinding:
    def __init__(self) -> None:
        self.refreshes = 0

    def refresh(self) -> None:
        self.refreshes += 1

    async def refresh_async(self) -> None:
        self.refreshes += 1


def _runner_with_counting_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FortifyRunner, _CountingBinding]:
    _silence_observability(monkeypatch)
    _install_fake_runner(monkeypatch)
    runner = FortifyRunner(
        agent=_make_agent(),
        app_name="app",
        session_service=InMemorySessionService(),
        api_key="k",
    )
    binding = _CountingBinding()
    runner._binding = binding  # type: ignore[assignment]
    return runner, binding


def test_run_refreshes_binding_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every run() pulls the policy before any event is yielded."""
    runner, binding = _runner_with_counting_binding(monkeypatch)

    list(runner.run(new_message="hi", user=_user()))
    list(runner.run(new_message="hi again", user=_user()))

    assert binding.refreshes == 2


@pytest.mark.asyncio
async def test_run_async_refreshes_binding_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, binding = _runner_with_counting_binding(monkeypatch)

    async for _ in runner.run_async(new_message="hi", user=_user()):
        pass

    assert binding.refreshes == 1


def test_construction_does_not_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve at construction is the initial pull; refresh only fires at
    run boundaries (the binding is freshly seeded — first run is a 304)."""
    runner, binding = _runner_with_counting_binding(monkeypatch)

    assert binding.refreshes == 0
