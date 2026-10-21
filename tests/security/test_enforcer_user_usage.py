"""The enforcer's ``user_usage.*`` wiring: enable per kind, read the context user's
ledger, and treat an anonymous caller as unavailable.

Every test injects its own :class:`UsageLedgers`. The process registry is one-way,
so enabling it here would leak into every later test.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hexgate.runtime.agent_usage import (
    UsageLedger,
    UsageLedgers,
    UsageScope,
    UsageScopeKind,
    new_usage_ledger,
)
from hexgate.runtime.context import HexgateContext
from hexgate.runtime.run_facts import run_scope
from hexgate.security import AGENT_RUN_TOOL, DecisionOutcome, Verdict
from hexgate.security.enforcer import PolicyEnforcer
from hexgate.security.policy_set import PolicySet, load_policy_set_from_dict
from hexgate.security.usage_inputs import NO_USAGE, UsageInputs
from hexgate.security.usage_source import OnUnavailable, UsageReading, local_reading

_AGENT = "billing"
_OTHER_AGENT = "support"
_START = 1_000_000.0
_ALICE = "alice"
_BOB = "bob"
_INVOCATIONS = "invocations_1h"


class _FakeClock:
    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> float:
        return self.now


def _ledgers(clock: _FakeClock | None = None) -> UsageLedgers:
    clock = clock or _FakeClock()
    return UsageLedgers(lambda: new_usage_ledger(clock=clock))


def _usage_block(on_unavailable: OnUnavailable | None) -> dict[str, Any]:
    if on_unavailable is None:
        return {}
    return {"usage": {"on_unavailable": on_unavailable.value}}


def _user_admission_cap(
    limit: int, *, on_unavailable: OnUnavailable | None = None
) -> PolicySet:
    return load_policy_set_from_dict(
        {
            "admission": {
                "mode": "allow",
                "constraints": [f"user_usage.{_INVOCATIONS} < {limit}"],
            },
            **_usage_block(on_unavailable),
        }
    )


def _agent_admission_cap(limit: int) -> PolicySet:
    return load_policy_set_from_dict(
        {
            "admission": {
                "mode": "allow",
                "constraints": [f"agent_usage.{_INVOCATIONS} < {limit}"],
            }
        }
    )


def _context(user_id: str) -> HexgateContext:
    return HexgateContext(user_id=user_id)


def _admit(
    enforcer: PolicyEnforcer, ledgers: UsageLedgers, agent: str = _AGENT
) -> bool:
    # Admission is decided before run_scope opens, with RunFacts DETACHED.
    allowed = enforcer.decide(AGENT_RUN_TOOL, {"agent": agent}).allowed
    if allowed:
        with run_scope(agent, ledgers=ledgers):
            pass
    return allowed


class _RecordingEngine:
    """Declares user usage paths, denies, and keeps every input it was handed."""

    def __init__(self, user_usage_paths: frozenset[str]) -> None:
        self._user_usage_paths = user_usage_paths
        self.inputs: list[UsageInputs] = []

    def agent_usage_paths(self) -> frozenset[str]:
        return frozenset()

    def user_usage_paths(self) -> frozenset[str]:
        return self._user_usage_paths

    def evaluate(
        self,
        *,
        role: str | None,
        tool: str,
        args: Mapping[str, Any],
        attributes: Mapping[str, Any] | None = None,
        run: Mapping[str, Any] | None = None,
        usage: UsageInputs = NO_USAGE,
    ) -> Verdict:
        self.inputs.append(usage)
        return Verdict(outcome=DecisionOutcome.DENY, reason="recorded")


class _EvaluateOnlyEngine:
    """A third-party engine written before either usage-paths method existed."""

    def __init__(self) -> None:
        self.inputs: list[UsageInputs] = []

    def evaluate(self, **kwargs: Any) -> Verdict:
        self.inputs.append(kwargs["usage"])
        return Verdict(outcome=DecisionOutcome.ALLOW)


class _RecordingSource:
    """A usage source that reads the ledger alone and keeps every scope it saw."""

    def __init__(self) -> None:
        self.scopes: list[UsageScope] = []

    def read(
        self,
        scope: UsageScope,
        paths: frozenset[str],
        ledger: UsageLedger,
        *,
        on_unavailable: OnUnavailable = OnUnavailable.ALLOW,
        current_run_age: float | None = None,
    ) -> UsageReading:
        self.scopes.append(scope)
        return local_reading(ledger, paths, current_run_age=current_run_age)


def test_binding_a_user_policy_enables_users_only() -> None:
    ledgers = _ledgers()

    PolicyEnforcer(_user_admission_cap(2), agent_name=_AGENT, ledgers=ledgers)

    assert ledgers.is_enabled(UsageScopeKind.USER)
    assert not ledgers.is_enabled(UsageScopeKind.AGENT)


def test_an_agent_only_policy_never_enables_users() -> None:
    ledgers = _ledgers()

    PolicyEnforcer(_agent_admission_cap(2), agent_name=_AGENT, ledgers=ledgers)

    assert ledgers.is_enabled(UsageScopeKind.AGENT)
    assert not ledgers.is_enabled(UsageScopeKind.USER)


def test_one_user_at_the_cap_leaves_another_admitted() -> None:
    ledgers = _ledgers()
    enforcer = PolicyEnforcer(
        _user_admission_cap(2), agent_name=_AGENT, ledgers=ledgers
    )

    with _context(_ALICE).sync_scope():
        alice = [_admit(enforcer, ledgers) for _ in range(3)]
    with _context(_BOB).sync_scope():
        bob = _admit(enforcer, ledgers)

    assert alice == [True, True, False]
    assert bob


def test_a_users_runs_on_another_agent_count() -> None:
    ledgers = _ledgers()
    other = PolicyEnforcer(
        _user_admission_cap(1), agent_name=_OTHER_AGENT, ledgers=ledgers
    )
    enforcer = PolicyEnforcer(
        _user_admission_cap(1), agent_name=_AGENT, ledgers=ledgers
    )

    with _context(_ALICE).sync_scope():
        assert _admit(other, ledgers, _OTHER_AGENT)
        assert not _admit(enforcer, ledgers)


def test_inside_the_run_the_cap_reads_as_at_admission() -> None:
    policy = load_policy_set_from_dict(
        {
            "admission": {
                "mode": "allow",
                "constraints": [f"user_usage.{_INVOCATIONS} < 1"],
            },
            "tools": {
                "refund": {
                    "mode": "allow",
                    "constraints": [f"user_usage.{_INVOCATIONS} < 1"],
                }
            },
        }
    )
    ledgers = _ledgers()
    enforcer = PolicyEnforcer(policy, agent_name=_AGENT, ledgers=ledgers)

    with _context(_ALICE).sync_scope():
        assert enforcer.decide(AGENT_RUN_TOOL, {"agent": _AGENT}).allowed
        with run_scope(_AGENT, ledgers=ledgers):
            assert enforcer.decide("refund", {}).allowed


def test_a_nested_admission_counts_the_enclosing_run() -> None:
    ledgers = _ledgers()
    parent = PolicyEnforcer(_user_admission_cap(1), agent_name=_AGENT, ledgers=ledgers)
    child = PolicyEnforcer(
        _user_admission_cap(1), agent_name=_OTHER_AGENT, ledgers=ledgers
    )

    with _context(_ALICE).sync_scope():
        assert parent.decide(AGENT_RUN_TOOL, {"agent": _AGENT}).allowed
        with run_scope(_AGENT, ledgers=ledgers):
            assert not child.decide(AGENT_RUN_TOOL, {"agent": _OTHER_AGENT}).allowed


def test_an_agent_admitted_inside_its_own_run_counts_that_run() -> None:
    ledgers = _ledgers()
    enforcer = PolicyEnforcer(
        _agent_admission_cap(1), agent_name=_AGENT, ledgers=ledgers
    )

    assert enforcer.decide(AGENT_RUN_TOOL, {"agent": _AGENT}).allowed
    with run_scope(_AGENT, ledgers=ledgers):
        assert not enforcer.decide(AGENT_RUN_TOOL, {"agent": _AGENT}).allowed


def test_another_users_context_inside_the_run_reads_that_user() -> None:
    ledgers = _ledgers()
    engine = _RecordingEngine(frozenset({_INVOCATIONS}))
    enforcer = PolicyEnforcer(engine, agent_name=_AGENT, ledgers=ledgers)

    with _context(_BOB).sync_scope(), run_scope(_AGENT, ledgers=ledgers):
        pass
    with _context(_ALICE).sync_scope(), run_scope(_AGENT, ledgers=ledgers):
        with _context(_BOB).sync_scope():
            enforcer.decide("refund", {})
        enforcer.decide("refund", {})

    assert [usage.user_usage for usage in engine.inputs] == [
        {_INVOCATIONS: 1},
        {_INVOCATIONS: 0},
    ]


def test_an_anonymous_caller_reads_zeros_under_allow() -> None:
    ledgers = _ledgers()
    enforcer = PolicyEnforcer(
        _user_admission_cap(1), agent_name=_AGENT, ledgers=ledgers
    )

    with _context("").sync_scope():
        admitted = [_admit(enforcer, ledgers) for _ in range(10)]

    assert all(admitted)
    assert not ledgers._user_ledgers


def test_no_context_is_anonymous() -> None:
    ledgers = _ledgers()
    engine = _RecordingEngine(frozenset({_INVOCATIONS}))
    enforcer = PolicyEnforcer(engine, agent_name=_AGENT, ledgers=ledgers)

    enforcer.decide("refund", {})

    assert engine.inputs[0].user_usage == {_INVOCATIONS: 0}
    assert not ledgers._user_ledgers


def test_an_anonymous_caller_fails_closed_under_deny() -> None:
    policy = load_policy_set_from_dict(
        {
            "tools": {
                "capped": {
                    "mode": "allow",
                    "constraints": [f"user_usage.{_INVOCATIONS} < 50"],
                },
                "plain": {"mode": "allow", "constraints": ["args.n < 5"]},
            },
            **_usage_block(OnUnavailable.DENY),
        }
    )
    ledgers = _ledgers()
    enforcer = PolicyEnforcer(policy, agent_name=_AGENT, ledgers=ledgers)

    with _context("").sync_scope():
        capped = enforcer.decide("capped", {})
        plain = enforcer.decide("plain", {"n": 1})

    assert not capped.allowed
    assert plain.allowed
    assert not ledgers._user_ledgers


def test_named_users_read_locally_even_with_a_source() -> None:
    policy = load_policy_set_from_dict(
        {
            "admission": {
                "mode": "allow",
                "constraints": [
                    f"agent_usage.{_INVOCATIONS} < 100",
                    f"user_usage.{_INVOCATIONS} < 2",
                ],
            },
            **_usage_block(OnUnavailable.DENY),
        }
    )
    ledgers = _ledgers()
    source = _RecordingSource()
    enforcer = PolicyEnforcer(
        policy,
        agent_name=_AGENT,
        ledgers=ledgers,
        usage_source=source,  # type: ignore[arg-type]
    )

    with _context(_ALICE).sync_scope():
        assert _admit(enforcer, ledgers)

    assert source.scopes == [UsageScope.agent(_AGENT)]


def test_a_user_only_policy_has_no_usage_refresh() -> None:
    enforcer = PolicyEnforcer(
        _user_admission_cap(2),
        agent_name=_AGENT,
        ledgers=_ledgers(),
        usage_source=_RecordingSource(),  # type: ignore[arg-type]
    )

    assert enforcer.usage_refresh() is None


def test_a_user_only_decision_has_no_usage_state() -> None:
    enforcer = PolicyEnforcer(
        _user_admission_cap(2), agent_name=_AGENT, ledgers=_ledgers()
    )

    with _context(_ALICE).sync_scope():
        decision = enforcer.decide(AGENT_RUN_TOOL, {"agent": _AGENT})

    assert decision.usage_state is None


def test_every_role_reads_one_user_namespace() -> None:
    engine = _RecordingEngine(frozenset({_INVOCATIONS}))
    enforcer = PolicyEnforcer(engine, agent_name=_AGENT, ledgers=_ledgers())

    with HexgateContext(user_id=_ALICE, user_roles=["a", "b", "c"]).sync_scope():
        enforcer.decide("refund", {})

    first = engine.inputs[0]
    assert len(engine.inputs) == 3
    assert all(usage is first for usage in engine.inputs)
    assert first.user_usage == {_INVOCATIONS: 0}


def test_an_engine_without_user_usage_paths_gets_no_user_namespace() -> None:
    engine = _EvaluateOnlyEngine()
    ledgers = _ledgers()
    enforcer = PolicyEnforcer(engine, agent_name=_AGENT, ledgers=ledgers)

    with _context(_ALICE).sync_scope():
        enforcer.decide("refund", {})

    assert engine.inputs[0].user_usage is None
    assert not ledgers.is_enabled(UsageScopeKind.USER)
