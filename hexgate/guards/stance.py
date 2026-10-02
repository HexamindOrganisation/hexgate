"""Validate a policy's guard stance against the agent's declared guards (R-GUARD-007).

The enable/disable stance a policy authors in its ``guards:`` block is *applied* per
call in the guarded runner (:mod:`hexgate.guards.runner`), which reads
``enforcer.policy.effective_guards(tool)`` and skips a guard the policy disabled —
uniform across every framework and live on the next call after a policy refresh.

This module holds only the *fail-fast* half: at agent construction, reject a policy
that governs a guard the agent does not declare, or a name attached more than once
(ambiguous). Catching it here turns a would-be silent misconfiguration into a loud
error at build. A *later* refresh that names an unknown guard is not re-checked here;
it degrades to a safe no-op at runtime (the name matches no guard) and the analyzer
lint (``hexgate policy validate``) catches it at authoring time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hexgate.guards.types import Guard


class GuardClosedWorldError(ValueError):
    """A policy governs a guard the agent's manifest does not declare, or one attached
    more than once (R-GUARD-007). Fail-loud at construction: silently ignoring it would
    let an operator believe a guard is governed when nothing enforces it, and it usually
    means a typo'd guard name or a guard deleted from the code."""


@runtime_checkable
class _GuardStanceEngine(Protocol):
    """The slice of a policy engine (bundle or set) that names governed guards."""

    def governed_guard_names(self) -> "frozenset[str]": ...


def validate_guard_policy(
    engine: object,
    guards: "Sequence[Guard] | None",
    *,
    agent_name: str | None = None,
) -> None:
    """Fail-fast closed-world check of the policy's guard stance at construction.

    Raises :class:`GuardClosedWorldError` when the policy governs a guard absent from
    ``guards`` (the agent's declared set), or a name that ``guards`` attaches more than
    once (a policy cannot address them separately). A no-op when the engine carries no
    guard stance (a guards-free policy, an older bundle, or an engine type without the
    reader). Does not touch the pipeline: enable/disable is applied per call in the
    runner, not baked here.
    """
    if not isinstance(engine, _GuardStanceEngine):
        return
    governed = engine.governed_guard_names()
    if not governed:
        return
    who = f" for agent {agent_name!r}" if agent_name else ""
    labels = [g.label for g in (guards or ())]
    missing = governed - set(labels)
    if missing:
        raise GuardClosedWorldError(
            f"policy{who} governs guard(s) {sorted(missing)} that the agent does not "
            "declare; a guard must be attached to the agent (and so on its manifest) "
            "before a policy can enable/disable it. Check for a typo or a removed guard."
        )
    ambiguous = sorted(
        {lbl for lbl in labels if lbl in governed and labels.count(lbl) > 1}
    )
    if ambiguous:
        raise GuardClosedWorldError(
            f"policy{who} governs guard name(s) {ambiguous} attached more than once; a "
            "policy cannot address them separately — give each guard a distinct "
            "function name."
        )
