"""Tests for the FortifyRunner that wraps the OpenAI Agents Runner."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from agents import Agent, FunctionTool

from fortify.adapters.openai import runner as runner_mod
from fortify.adapters.openai.runner import FortifyRunner
from fortify.runtime import User
from fortify.runtime.context import get_current_user
from fortify.security import AgentPolicy, BaseToolPolicy, PolicyBinding, PolicySet
from fortify.security.enforcer import PolicyEnforcer
from fortify.security.policy_set import DEFAULT_ROLE_NAME


@pytest.fixture(autouse=True)
def _stub_resolve(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the platform resolve seam — runner tests are about lifecycle,
    not policy resolution (covered by test_wrapper.py / binding tests).
    Returns the list of resolved agent names so tests can assert on the
    binding cache."""
    resolved_names: list[str] = []

    def fake_resolve(agent: Any, name: str, key: str) -> PolicyBinding:
        resolved_names.append(name)
        tool_names = [t.name for t in (getattr(agent, "tools", []) or [])]
        engine = PolicySet(
            {
                DEFAULT_ROLE_NAME: AgentPolicy(
                    tools={n: BaseToolPolicy(mode="allow") for n in tool_names}
                )
            }
        )
        return PolicyBinding(PolicyEnforcer(engine, agent_name=name))

    monkeypatch.setattr(runner_mod, "_resolve_binding", fake_resolve)
    return resolved_names


def _user() -> User:
    """Build a minimal User for runner tests."""
    return User(user_id="u-1", session_id="s-1", role="developer")


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
async def test_run_wraps_agent_opens_user_scope_and_calls_runner_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run() wraps the agent, opens the User scope, and forwards to Runner.run."""
    setup_counts = _silence_observability(monkeypatch)
    captured: dict[str, Any] = {}

    async def fake_run(starting_agent: Agent, input: Any, **kwargs: Any) -> str:
        captured["agent"] = starting_agent
        captured["input"] = input
        captured["kwargs"] = kwargs
        captured["active_user"] = get_current_user()
        return "run-result"

    monkeypatch.setattr(
        "fortify.adapters.openai.runner.Runner.run", staticmethod(fake_run)
    )

    runner = FortifyRunner(api_key="k")
    agent = _make_agent()
    user = _user()

    result = await runner.run(agent, "hello", user=user)

    assert result == "run-result"
    assert setup_counts["setup"] == 1
    assert captured["agent"] is not agent
    assert captured["agent"].name == agent.name
    assert captured["input"] == "hello"
    assert captured["kwargs"] == {"run_config": None}
    # User scope was live for the duration of Runner.run.
    assert captured["active_user"] is user
    # Scope unwound on exit — no leak.
    assert get_current_user() is None


def test_run_sync_opens_user_scope_and_calls_runner_run_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_sync() opens the User scope via sync_scope and forwards to Runner.run_sync."""
    setup_counts = _silence_observability(monkeypatch)
    captured: dict[str, Any] = {}

    def fake_run_sync(starting_agent: Agent, input: Any, **kwargs: Any) -> str:
        captured["agent"] = starting_agent
        captured["input"] = input
        captured["kwargs"] = kwargs
        captured["active_user"] = get_current_user()
        return "run-sync-result"

    monkeypatch.setattr(
        "fortify.adapters.openai.runner.Runner.run_sync", staticmethod(fake_run_sync)
    )

    runner = FortifyRunner(api_key="k")
    agent = _make_agent()
    user = _user()

    result = runner.run_sync(agent, "hello", user=user)

    assert result == "run-sync-result"
    assert setup_counts["setup"] == 1
    assert captured["agent"] is not agent
    assert captured["input"] == "hello"
    assert captured["active_user"] is user
    assert get_current_user() is None


@pytest.mark.asyncio
async def test_run_streamed_wraps_stream_events_to_re_enter_scope_and_propagation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_streamed swaps stream_events for a wrapper that re-enters User + propagation."""
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
    user = _user()

    result = runner.run_streamed(agent, "hello", user=user)

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
async def test_run_propagates_user_identity_to_langfuse(
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

    await runner.run(_make_agent("custom-name"), "hi", user=_user())

    [call] = propagate_calls
    assert call["tags"] == ["openai.runner.run.custom-name"]
    assert call["user_id"] == "u-1"
    assert call["session_id"] == "s-1"
    assert call["metadata"] == {"user_role": "developer"}


# ---------------------------------------------------------------------------
# Binding cache + per-run refresh (phase 6)
# ---------------------------------------------------------------------------


class _CountingBinding:
    def __init__(self) -> None:
        self.refreshes = 0
        self.enforcer = PolicyEnforcer(
            PolicySet(
                {
                    DEFAULT_ROLE_NAME: AgentPolicy(
                        tools={"echo": BaseToolPolicy(mode="allow")}
                    )
                }
            ),
            agent_name="my-agent",
        )

    def refresh(self) -> None:
        self.refreshes += 1

    async def refresh_async(self) -> None:
        self.refreshes += 1


def _patch_runner_run(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(*_args: Any, **_kwargs: Any) -> str:
        return "ok"

    monkeypatch.setattr(
        "fortify.adapters.openai.runner.Runner.run", staticmethod(fake_run)
    )


@pytest.mark.asyncio
async def test_binding_is_cached_per_agent_name(
    monkeypatch: pytest.MonkeyPatch, _stub_resolve: list[str]
) -> None:
    """Same agent name across runs → one resolve; the ETag memory lives in
    the cached binding's source, not in a per-call construction."""
    _silence_observability(monkeypatch)
    _patch_runner_run(monkeypatch)

    runner = FortifyRunner(api_key="k")
    agent = _make_agent("my-agent")

    await runner.run(agent, "one", user=_user())
    await runner.run(agent, "two", user=_user())

    assert _stub_resolve == ["my-agent"]


@pytest.mark.asyncio
async def test_distinct_agent_names_get_distinct_bindings(
    monkeypatch: pytest.MonkeyPatch, _stub_resolve: list[str]
) -> None:
    _silence_observability(monkeypatch)
    _patch_runner_run(monkeypatch)

    runner = FortifyRunner(api_key="k")

    await runner.run(_make_agent("agent-a"), "x", user=_user())
    await runner.run(_make_agent("agent-b"), "x", user=_user())

    assert _stub_resolve == ["agent-a", "agent-b"]
    assert set(runner._bindings) == {"agent-a", "agent-b"}


def test_binding_for_normalises_none_agent_name_to_default(
    _stub_resolve: list[str],
) -> None:
    """A None agent name must not flow through as the cache key or the
    agent_name handed to the platform resolve seam — it collapses to the
    same "default" label the other adapters use, never a null identity.

    Exercises ``_binding_for`` directly: the canonical openai ``Agent``
    validates ``name`` as a string at construction, so this guards the
    normalisation for stand-in / subclassed agents that don't."""
    runner = FortifyRunner(api_key="k")
    agent = SimpleNamespace(name=None, tools=[_make_tool("echo")])

    binding = runner._binding_for(agent)  # type: ignore[arg-type]

    assert _stub_resolve == ["default"]
    assert set(runner._bindings) == {"default"}
    assert None not in runner._bindings
    assert binding.enforcer.agent_name == "default"


@pytest.mark.asyncio
async def test_run_refreshes_cached_binding_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _silence_observability(monkeypatch)
    _patch_runner_run(monkeypatch)

    runner = FortifyRunner(api_key="k")
    binding = _CountingBinding()
    runner._bindings["my-agent"] = binding  # type: ignore[assignment]

    await runner.run(_make_agent("my-agent"), "one", user=_user())
    await runner.run(_make_agent("my-agent"), "two", user=_user())

    assert binding.refreshes == 2


def test_run_sync_refreshes_cached_binding_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _silence_observability(monkeypatch)

    def fake_run_sync(*_args: Any, **_kwargs: Any) -> str:
        return "ok"

    monkeypatch.setattr(
        "fortify.adapters.openai.runner.Runner.run_sync",
        staticmethod(fake_run_sync),
    )

    runner = FortifyRunner(api_key="k")
    binding = _CountingBinding()
    runner._bindings["my-agent"] = binding  # type: ignore[assignment]

    runner.run_sync(_make_agent("my-agent"), "one", user=_user())

    assert binding.refreshes == 1


def test_run_streamed_refreshes_before_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refresh must land before Runner.run_streamed fixes the wrap —
    tools fire later during stream_events, against whatever the enforcer
    holds at setup."""
    _silence_observability(monkeypatch)

    order: list[str] = []

    def fake_run_streamed(*_args: Any, **_kwargs: Any) -> _FakeStreamingResult:
        order.append("run_streamed")
        return _FakeStreamingResult()

    monkeypatch.setattr(
        "fortify.adapters.openai.runner.Runner.run_streamed",
        staticmethod(fake_run_streamed),
    )

    class _OrderedBinding(_CountingBinding):
        def refresh(self) -> None:
            order.append("refresh")
            super().refresh()

    runner = FortifyRunner(api_key="k")
    binding = _OrderedBinding()
    runner._bindings["my-agent"] = binding  # type: ignore[assignment]

    runner.run_streamed(_make_agent("my-agent"), "hello", user=_user())

    assert order == ["refresh", "run_streamed"]
