"""The enforcer's ``agent_usage.*`` wiring: enable on bind, one namespace per decision.

Every test injects its own :class:`UsageLedgers`. The process registry is one-way,
so enabling it here would leak into every later test.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hexgate.runtime.agent_usage import UsageLedgers, new_usage_ledger
from hexgate.runtime.context import HexgateContext
from hexgate.runtime.run_facts import get_run_facts, run_scope
from hexgate.security import AGENT_RUN_TOOL, DecisionOutcome, Verdict
from hexgate.security.enforcer import PolicyEnforcer
from hexgate.security.policy_set import PolicySet, load_policy_set_from_dict

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
