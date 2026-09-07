"""Assertion helpers for unit-testing policies.

Wrap the same evaluation path the SDK enforces with, so a policy authored in
code (or YAML) can be exercised in a pytest suite:

    from hexgate.security import PolicyBuilder, C, assert_allows, assert_denies

    policy = PolicyBuilder().allow("refund", when=[C("args.amount") <= 500]).build()
    assert_allows(policy, "refund", {"amount": 100})
    assert_denies(policy, "refund", {"amount": 999})

``policy`` may be an :class:`AgentPolicy` (single role) or a :class:`PolicySet`
(role-aware — pass ``role=``).

Assert a ``run.*`` cap by supplying the run's facts via :func:`run_namespace`:

    assert_denies(policy, "refund", run=run_namespace("refund", tool_calls=20))
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from hexgate.runtime.run_facts import KNOWN_RUN_PATHS, RUN_PATH_TYPES, RunFacts
from hexgate.security.decision import DecisionOutcome
from hexgate.security.models import AgentPolicy
from hexgate.security.policy import evaluate_tool_call
from hexgate.security.policy_set import PolicySet

Policy = AgentPolicy | PolicySet

_TOOL_CALLS = "tool_calls"
_CALLS_OF_THIS_TOOL = "calls_of_this_tool"
_TOOLS_USED = "tools_used"
_TOTAL_TOKENS = "total_tokens"
_TOKEN_SPLIT = ("input_tokens", "output_tokens")


def run_namespace(tool: str = "", **facts: Any) -> dict[str, Any]:
    """A ``run`` namespace with ``facts`` applied over a zeroed run.

    ``tool`` is the tool the assertion decides on. It keys the per-tool view,
    so ``calls_of_this_tool`` and ``tools_used`` describe that tool instead of
    reading empty against a ``tool_calls`` the caller did supply. Naming it
    reads the run as single-tool — every call so far went to ``tool`` — which
    is what a policy unit test usually means; pass ``calls_of_this_tool``
    explicitly for a mixed run.

    ``facts`` win over both the zeroed base and the derivation, so a derived
    path can still be set outright.

    Raises on an unregistered keyword — a typo like ``tool_call=20`` would
    otherwise leave the real counter at 0 and the cap would never fire — and on
    a wrong-typed value, which fails the comparison closed and is then
    indistinguishable from the cap firing for real.
    """
    unknown = sorted(set(facts) - KNOWN_RUN_PATHS)
    if unknown:
        raise ValueError(
            f"unknown run.* path(s) {unknown} "
            f"(this build knows: {', '.join(sorted(KNOWN_RUN_PATHS))})"
        )
    for name, value in facts.items():
        _check_run_value(name, value)
    namespace = {**_seeded_run(tool, facts), **facts}
    _apply_token_total(namespace, facts)
    return namespace


def _seeded_run(tool: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    """A zeroed run with ``tool`` credited for the calls ``facts`` describes.

    Without this the per-tool view is empty whatever ``tool`` is passed, so
    ``run_namespace("refund", tool_calls=20)`` claims twenty calls alongside
    ``tools_used == []`` — a state no real run reaches, and a
    ``run.calls_of_this_tool`` cap tested against it reads 0 and never fires.
    """
    namespace = _zeroed_run(tool)
    calls = _credited_calls(tool, facts)
    if calls:
        namespace[_CALLS_OF_THIS_TOOL] = calls
        namespace[_TOOLS_USED] = [tool]
    return namespace


def _credited_calls(tool: str, facts: Mapping[str, Any]) -> int:
    """Executions to credit ``tool`` with: its own count when given, else the
    run-wide one. Zero for an unnamed tool — there is nothing to key by."""
    if not tool:
        return 0
    return facts.get(_CALLS_OF_THIS_TOOL, facts.get(_TOOL_CALLS, 0))


def _apply_token_total(namespace: dict[str, Any], facts: Mapping[str, Any]) -> None:
    """Derive ``total_tokens`` from a supplied token split.

    ``RunFacts.as_namespace`` derives it in production. Merged flat it would
    keep the zeroed 0 while the split reads non-zero, so a
    ``run.total_tokens`` cap would pass in the test and fire in production.
    """
    if _TOTAL_TOKENS in facts or not any(name in facts for name in _TOKEN_SPLIT):
        return
    namespace[_TOTAL_TOKENS] = sum(namespace[name] for name in _TOKEN_SPLIT)


def _check_run_value(name: str, value: Any) -> None:
    """Raise unless ``value`` has the shape ``run.<name>`` projects."""
    expected = RUN_PATH_TYPES[name]
    if not _matches_run_type(value, expected):
        raise ValueError(
            f"run.{name} expects {expected.__name__}, got "
            f"{type(value).__name__} ({value!r})"
        )


def _matches_run_type(value: Any, expected: type) -> bool:
    # bool is an int subclass, but ``{"tool_calls": true}`` is a JSON typo
    # rather than a count, so it satisfies no numeric path.
    if isinstance(value, bool):
        return expected is bool
    # An int is an acceptable float (``elapsed_seconds=300``), not vice versa.
    if expected is float:
        return type(value) in (int, float)
    return type(value) is expected


def _zeroed_run(tool: str) -> dict[str, Any]:
    """A freshly-started run — the default so an unset ``run.*`` cap reads
    zero instead of failing closed."""
    return RunFacts(id=str(uuid4()), agent="").as_namespace(tool)


def _outcome(
    policy: Policy,
    tool: str,
    args: dict[str, Any] | None,
    role: str | None,
    attributes: dict[str, Any] | None,
    run: Mapping[str, Any] | None,
) -> DecisionOutcome:
    resolved_run = run if run is not None else _zeroed_run(tool)
    if isinstance(policy, PolicySet):
        return policy.evaluate(
            role=role,
            tool=tool,
            args=args or {},
            attributes=attributes,
            run=resolved_run,
        ).outcome
    return evaluate_tool_call(
        policy, tool, args or {}, role=role, attributes=attributes, run=resolved_run
    ).outcome


def _check(
    policy: Policy,
    tool: str,
    args: dict[str, Any] | None,
    role: str | None,
    attributes: dict[str, Any] | None,
    run: Mapping[str, Any] | None,
    expected: DecisionOutcome,
) -> None:
    actual = _outcome(policy, tool, args, role, attributes, run)
    if actual is not expected:
        scope = f"role={role!r} " if role is not None else ""
        raise AssertionError(
            f"expected {expected.value} for {scope}{tool}({args or {}}), "
            f"got {actual.value}"
        )


def assert_allows(
    policy: Policy,
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    role: str | None = None,
    attributes: dict[str, Any] | None = None,
    run: Mapping[str, Any] | None = None,
) -> None:
    """Assert the policy ALLOWS this call.

    ``attributes`` and ``run`` feed ``ctx.*`` and ``run.*`` constraints; ``run``
    defaults to a freshly-started run — see :func:`run_namespace` to set one."""
    _check(policy, tool, args, role, attributes, run, DecisionOutcome.ALLOW)


def assert_denies(
    policy: Policy,
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    role: str | None = None,
    attributes: dict[str, Any] | None = None,
    run: Mapping[str, Any] | None = None,
) -> None:
    """Assert the policy DENIES this call."""
    _check(policy, tool, args, role, attributes, run, DecisionOutcome.DENY)


def assert_needs_approval(
    policy: Policy,
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    role: str | None = None,
    attributes: dict[str, Any] | None = None,
    run: Mapping[str, Any] | None = None,
) -> None:
    """Assert the policy routes this call to approval."""
    _check(policy, tool, args, role, attributes, run, DecisionOutcome.NEEDS_APPROVAL)
