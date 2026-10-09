"""The enforcer's ``agent_usage.*`` wiring: enable on bind, one namespace per decision.

Every test injects its own :class:`UsageLedgers`. The process registry is one-way,
so enabling it here would leak into every later test.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, Future
from typing import Any

import pytest

from hexgate.runtime.agent_usage import UsageLedgers, new_usage_ledger
from hexgate.runtime.context import HexgateContext
from hexgate.runtime.run_facts import get_run_facts, run_scope
from hexgate.security import AGENT_RUN_TOOL, DecisionOutcome, Verdict, usage_source
from hexgate.security.enforcer import PolicyEnforcer, UsageRefresh, build_enforcer
from hexgate.security.policy_set import PolicySet, load_policy_set_from_dict
from hexgate.security.usage_source import (
    OnUnavailable,
    PlatformUsageSource,
    UsageRefreshSettings,
    UsageState,
)
from hexgate.tracing import _senders

_AGENT = "billing"
_START = 1_000_000.0
_HOUR = 3_600.0
_MINUTE = 60.0
_ADMISSION_ARGS = {"agent": _AGENT}


class _FakeClock:
    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> float:
        return self.now


def _ledgers(clock: _FakeClock | None = None) -> UsageLedgers:
    clock = clock or _FakeClock()
    return UsageLedgers(lambda: new_usage_ledger(clock=clock))


def _admission_cap(limit: int) -> PolicySet:
    return load_policy_set_from_dict(
        {
            "admission": {
                "mode": "allow",
                "constraints": [f"agent_usage.invocations_1h < {limit}"],
            }
        }
    )


def _tool_cap(limit: int) -> PolicySet:
    return load_policy_set_from_dict(
        {
            "tools": {
                "refund": {
                    "mode": "allow",
                    "constraints": [f"agent_usage.tool_calls_5m < {limit}"],
                }
            }
        }
    )


def _usage_free() -> PolicySet:
    return load_policy_set_from_dict({"tools": {"refund": {"mode": "allow"}}})


class _RecordingEngine:
    """Declares usage paths, denies, and keeps every namespace it was handed."""

    def __init__(self, usage_paths: frozenset[str]) -> None:
        self._usage_paths = usage_paths
        self.namespaces: list[Mapping[str, Any] | None] = []

    def agent_usage_paths(self) -> frozenset[str]:
        return self._usage_paths

    def evaluate(
        self,
        *,
        role: str | None,
        tool: str,
        args: Mapping[str, Any],
        attributes: Mapping[str, Any] | None = None,
        run: Mapping[str, Any] | None = None,
        agent_usage: Mapping[str, Any] | None = None,
    ) -> Verdict:
        self.namespaces.append(agent_usage)
        return Verdict(outcome=DecisionOutcome.DENY, reason="recorded")


class _EvaluateOnlyEngine:
    """A third-party engine written before ``agent_usage_paths`` existed."""

    def __init__(self) -> None:
        self.namespaces: list[Mapping[str, Any] | None] = []

    def evaluate(self, **kwargs: Any) -> Verdict:
        self.namespaces.append(kwargs.get("agent_usage"))
        return Verdict(outcome=DecisionOutcome.ALLOW)


def test_binding_a_usage_policy_enables_the_ledgers() -> None:
    ledgers = _ledgers()

    PolicyEnforcer(_admission_cap(2), agent_name=_AGENT, ledgers=ledgers)

    assert ledgers.enabled


def test_binding_a_usage_free_policy_leaves_them_off() -> None:
    ledgers = _ledgers()

    PolicyEnforcer(_usage_free(), agent_name=_AGENT, ledgers=ledgers)

    assert not ledgers.enabled


def test_swapping_in_a_usage_policy_enables_the_ledgers() -> None:
    """The ``PolicyBinding.refresh`` path: a refresh adds the first usage path."""
    ledgers = _ledgers()
    enforcer = PolicyEnforcer(_usage_free(), agent_name=_AGENT, ledgers=ledgers)

    enforcer.policy = _tool_cap(2)

    assert ledgers.enabled
    assert enforcer.decide("refund", {}).allowed


def test_an_admission_cap_counts_runs_and_clears_as_they_age_out() -> None:
    clock = _FakeClock()
    ledgers = _ledgers(clock)
    enforcer = PolicyEnforcer(_admission_cap(2), agent_name=_AGENT, ledgers=ledgers)

    def admit() -> bool:
        # Admission is decided before run_scope opens, with RunFacts DETACHED.
        decision = enforcer.decide(AGENT_RUN_TOOL, _ADMISSION_ARGS)
        if decision.allowed:
            with run_scope(_AGENT, ledgers=ledgers):
                pass
        return decision.allowed

    assert [admit(), admit(), admit()] == [True, True, False]

    clock.now += _HOUR + _MINUTE
    assert admit()


def test_a_top_level_invocation_cap_reads_the_same_inside_the_run() -> None:
    """The last admitted run can still call tools: inside a run, its own invocation
    is left out, as it was at admission."""
    policy = load_policy_set_from_dict(
        {
            "constraints": ["agent_usage.invocations_1h < 2"],
            "admission": {"mode": "allow"},
            "tools": {"refund": {"mode": "allow"}},
        }
    )
    ledgers = _ledgers()
    enforcer = PolicyEnforcer(policy, agent_name=_AGENT, ledgers=ledgers)
    outcomes = []

    for _ in range(3):
        admitted = enforcer.decide(AGENT_RUN_TOOL, _ADMISSION_ARGS).allowed
        if admitted:
            with run_scope(_AGENT, ledgers=ledgers):
                admitted = enforcer.decide("refund", {}).allowed
        outcomes.append(admitted)

    assert outcomes == [True, True, False]


def test_another_agents_run_is_not_left_out() -> None:
    ledgers = _ledgers()
    enforcer = PolicyEnforcer(_tool_cap(2), agent_name=_AGENT, ledgers=ledgers)
    engine = _RecordingEngine(frozenset({"invocations_1h"}))
    enforcer.policy = engine

    with run_scope(_AGENT, ledgers=ledgers):
        pass
    with run_scope("other", ledgers=ledgers):
        enforcer.decide("refund", {})
    with run_scope(_AGENT, ledgers=ledgers):
        enforcer.decide("refund", {})

    assert engine.namespaces == [{"invocations_1h": 1}, {"invocations_1h": 1}]


def test_a_run_opened_before_the_ledgers_were_enabled_is_not_left_out() -> None:
    """It recorded no invocation, so subtracting one would hide another run's."""
    ledgers = _ledgers()
    enforcer = PolicyEnforcer(_usage_free(), agent_name=_AGENT, ledgers=ledgers)
    engine = _RecordingEngine(frozenset({"invocations_1h"}))

    with run_scope(_AGENT, ledgers=ledgers):
        enforcer.policy = engine
        for _ in range(2):
            with run_scope(_AGENT, ledgers=ledgers):
                pass
        enforcer.decide("refund", {})

    assert engine.namespaces == [{"invocations_1h": 2}]


def test_a_tool_cap_spans_runs() -> None:
    ledgers = _ledgers()
    enforcer = PolicyEnforcer(_tool_cap(2), agent_name=_AGENT, ledgers=ledgers)
    outcomes = []

    for _ in range(3):
        with run_scope(_AGENT, ledgers=ledgers):
            decision = enforcer.decide("refund", {})
            if decision.allowed:
                get_run_facts().record_execution("refund")
            outcomes.append(decision.allowed)

    assert outcomes == [True, True, False]


def test_every_role_sees_the_same_namespace() -> None:
    engine = _RecordingEngine(frozenset({"tool_calls_5m"}))
    enforcer = PolicyEnforcer(engine, agent_name=_AGENT, ledgers=_ledgers())

    with HexgateContext(user_id="u", user_roles=["a", "b", "c"]).sync_scope():
        enforcer.decide("refund", {})

    first = engine.namespaces[0]
    assert first == {"tool_calls_5m": 0}
    assert len(engine.namespaces) == 3
    assert all(namespace is first for namespace in engine.namespaces)


def test_an_engine_without_usage_paths_still_decides() -> None:
    engine = _EvaluateOnlyEngine()
    ledgers = _ledgers()
    enforcer = PolicyEnforcer(engine, agent_name=_AGENT, ledgers=ledgers)

    assert enforcer.decide("refund", {}).allowed
    assert engine.namespaces == [None]
    assert not ledgers.enabled


def test_a_usage_free_engine_gets_no_namespace_and_no_ledger() -> None:
    engine = _RecordingEngine(frozenset())
    ledgers = _ledgers()
    enforcer = PolicyEnforcer(engine, agent_name=_AGENT, ledgers=ledgers)

    enforcer.decide("refund", {})

    assert engine.namespaces == [None]
    assert ledgers.ledger_for(_AGENT) is None


def test_without_a_usage_source_a_decision_reads_local() -> None:
    enforcer = PolicyEnforcer(_tool_cap(2), agent_name=_AGENT, ledgers=_ledgers())

    assert enforcer.decide("refund", {}).usage_state == UsageState.LOCAL


# --- Through the platform usage source --------------------------------------------

_INVOCATIONS = "invocations_1h"
_ADMISSION_PATHS = frozenset({_INVOCATIONS})


class _FakeFetcher:
    """Answers the requested paths it has a value for, or raises ``error``."""

    def __init__(self, values: Mapping[str, int] | None = None) -> None:
        self.values = dict(values or {})
        self.error: Exception | None = None

    def get_agent_usage(self, name: str, paths: Sequence[str]) -> Mapping[str, Any]:
        if self.error is not None:
            raise self.error
        return {
            "values": {path: self.values[path] for path in paths if path in self.values}
        }


class _InlineExecutor(Executor):
    """Runs each background fetch on submit, on the caller's thread."""

    def submit(  # type: ignore[override]
        self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Future[Any]:
        future: Future[Any] = Future()
        future.set_result(fn(*args, **kwargs))
        return future


class _Platform:
    """A usage source and the ledgers it combines with, on one clock."""

    def __init__(self, values: Mapping[str, int] | None = None) -> None:
        self.clock = _FakeClock()
        self.fetcher = _FakeFetcher(values)
        self.ledgers = _ledgers(self.clock)
        self.source = PlatformUsageSource(
            self.fetcher, _InlineExecutor(), UsageRefreshSettings(), self.clock
        )

    def enforcer(self, policy: Any) -> PolicyEnforcer:
        return PolicyEnforcer(
            policy,
            agent_name=_AGENT,
            ledgers=self.ledgers,
            usage_source=self.source,
        )

    def boundary(self, enforcer: PolicyEnforcer) -> None:
        usage = enforcer.usage_refresh()
        assert usage is not None
        usage.run()


class _FailModeEngine:
    """Delegates to ``policy`` and declares 6b's fail mode."""

    def __init__(self, policy: PolicySet, on_unavailable: OnUnavailable) -> None:
        self._policy = policy
        self._on_unavailable = on_unavailable

    def agent_usage_paths(self) -> frozenset[str]:
        return self._policy.agent_usage_paths()

    def usage_on_unavailable(self) -> OnUnavailable:
        return self._on_unavailable

    def evaluate(self, **kwargs: Any) -> Verdict:
        return self._policy.evaluate(**kwargs)


class _UnreadSource:
    def read(self, *_: Any, **__: Any) -> None:
        raise AssertionError("a usage-free policy must not read the source")


def test_the_platform_term_is_counted() -> None:
    platform = _Platform({_INVOCATIONS: 99})
    enforcer = platform.enforcer(_admission_cap(100))
    platform.boundary(enforcer)
    with run_scope(_AGENT, ledgers=platform.ledgers):
        pass

    decision = enforcer.decide(AGENT_RUN_TOOL, _ADMISSION_ARGS)

    assert not decision.allowed
    assert decision.usage_state == UsageState.FRESH


def test_an_unreachable_platform_reads_ledger_only_and_admits() -> None:
    """G3: a platform outage never denies by default. A PolicySet has no fail-mode
    method until 6b, so this also pins the enforcer's allow fallback."""
    platform = _Platform()
    platform.fetcher.error = RuntimeError("platform down")
    enforcer = platform.enforcer(_admission_cap(2))
    platform.boundary(enforcer)

    decision = enforcer.decide(AGENT_RUN_TOOL, _ADMISSION_ARGS)

    assert decision.allowed
    assert decision.usage_state == UsageState.UNAVAILABLE


def test_a_path_added_by_a_policy_swap_reads_partial_and_does_not_deny() -> None:
    """G4 / F5: the snapshot predates the new path, which must not deny the call."""
    platform = _Platform({_INVOCATIONS: 0})
    enforcer = platform.enforcer(_admission_cap(5))
    platform.boundary(enforcer)
    enforcer.policy = load_policy_set_from_dict(
        {
            "constraints": [f"agent_usage.{_INVOCATIONS} < 5"],
            "tools": {
                "refund": {
                    "mode": "allow",
                    "constraints": ["agent_usage.tool_calls_5m < 5"],
                }
            },
        }
    )

    decision = enforcer.decide("refund", {})

    assert decision.allowed
    assert decision.usage_state == UsageState.PARTIAL


@pytest.mark.parametrize(
    ("on_unavailable", "admitted"),
    [(OnUnavailable.DENY, False), (OnUnavailable.ALLOW, True)],
)
def test_the_fail_mode_is_read_from_the_engine(
    on_unavailable: OnUnavailable, admitted: bool
) -> None:
    platform = _Platform()
    platform.fetcher.error = RuntimeError("platform down")
    enforcer = platform.enforcer(_FailModeEngine(_admission_cap(2), on_unavailable))
    platform.boundary(enforcer)

    assert enforcer.decide(AGENT_RUN_TOOL, _ADMISSION_ARGS).allowed is admitted


def test_no_usage_refresh_without_a_source() -> None:
    enforcer = PolicyEnforcer(_admission_cap(2), agent_name=_AGENT, ledgers=_ledgers())

    assert enforcer.usage_refresh() is None


def test_no_usage_refresh_for_a_usage_free_policy() -> None:
    assert _Platform().enforcer(_usage_free()).usage_refresh() is None


def test_the_usage_refresh_follows_the_bound_policy() -> None:
    platform = _Platform()
    enforcer = platform.enforcer(_admission_cap(2))

    assert enforcer.usage_refresh() == UsageRefresh(
        platform.source, _AGENT, _ADMISSION_PATHS
    )

    enforcer.policy = _tool_cap(2)

    assert enforcer.usage_refresh() == UsageRefresh(
        platform.source, _AGENT, frozenset({"tool_calls_5m"})
    )


def test_the_own_run_is_left_out_through_the_source() -> None:
    """Twin of ``test_a_top_level_invocation_cap_reads_the_same_inside_the_run``."""
    policy = load_policy_set_from_dict(
        {
            "constraints": [f"agent_usage.{_INVOCATIONS} < 2"],
            "admission": {"mode": "allow"},
            "tools": {"refund": {"mode": "allow"}},
        }
    )
    platform = _Platform({_INVOCATIONS: 0})
    enforcer = platform.enforcer(policy)
    platform.boundary(enforcer)
    outcomes = []

    for _ in range(3):
        admitted = enforcer.decide(AGENT_RUN_TOOL, _ADMISSION_ARGS).allowed
        if admitted:
            with run_scope(_AGENT, ledgers=platform.ledgers):
                admitted = enforcer.decide("refund", {}).allowed
        outcomes.append(admitted)

    assert outcomes == [True, True, False]


def test_a_usage_free_policy_never_reads_the_source() -> None:
    enforcer = PolicyEnforcer(
        _usage_free(),
        agent_name=_AGENT,
        ledgers=_ledgers(),
        usage_source=_UnreadSource(),  # type: ignore[arg-type]
    )

    assert enforcer.decide("refund", {}).usage_state is None


_KEY = "fty_test_demo_dummybiscuit"


@pytest.fixture
def _platform_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    from hexgate.security import enforcer as enforcer_mod

    monkeypatch.setattr(usage_source, "_usage_sources", {})
    # Audit is not under test; a real sender would outlive the test.
    monkeypatch.setattr(enforcer_mod, "configure", lambda _api_key: None)
    for name in ("HEXGATE_API_KEY", "HEXGATE_LOCAL_POLICY", _senders._LOCAL_MODE_ENV):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_build_enforcer_with_a_key_has_a_usage_source(
    _platform_env: pytest.MonkeyPatch,
) -> None:
    enforcer = build_enforcer(_usage_free(), agent_name=_AGENT, api_key=_KEY)

    assert isinstance(enforcer._usage_source, PlatformUsageSource)


def test_build_enforcer_without_a_key_has_none(
    _platform_env: pytest.MonkeyPatch,
) -> None:
    enforcer = build_enforcer(_usage_free(), agent_name=_AGENT)

    assert enforcer._usage_source is None


def test_build_enforcer_in_local_mode_has_none(
    _platform_env: pytest.MonkeyPatch,
) -> None:
    _platform_env.setenv(_senders._LOCAL_MODE_ENV, "1")

    enforcer = build_enforcer(_usage_free(), agent_name=_AGENT, api_key=_KEY)

    assert enforcer._usage_source is None
