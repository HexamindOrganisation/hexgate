"""Tool-shape-agnostic policy enforcement.

:class:`PolicyEnforcer` returns a :class:`Decision` for a proposed tool
call and stops — adapters translate it for their host. Stateless across
calls: each :meth:`decide` re-reads the active :class:`HexgateContext`
from the contextvar.

Multi-role callers are handled here, not in the engines: :meth:`decide` folds
one verdict per role through
:func:`~hexgate.security.decision.combine_role_verdicts`, leaving both engines
single-role.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hexgate.audit import AuditEvent, configure
from hexgate.runtime.agent_usage import AGENT_USAGE_LEDGERS, UsageLedgers
from hexgate.runtime.context import get_current_context
from hexgate.runtime.roles import resolve_role_set
from hexgate.runtime.run_facts import get_run_facts
from hexgate.security.decision import (
    Decision,
    PolicyEngine,
    RunAttribution,
    combine_role_verdicts,
)
from hexgate.security.usage_source import (
    OnUnavailable,
    PlatformUsageSource,
    UsageReading,
    local_reading,
    resolve_usage_source,
)
from hexgate.tracing._senders import AuditSender

if TYPE_CHECKING:
    from hexgate.runtime.context import HexgateContext
    from hexgate.runtime.run_facts import RunFacts

_log = logging.getLogger(__name__)

# run.* path the agent_usage namespace reads to age out the current run's invocation.
_RUN_ELAPSED = "elapsed_seconds"

_warned_role_cap = False


def _warn_role_cap(total: int, kept: int) -> None:
    """Warn once per process: the cap silently narrows what a caller can do, but
    re-logging it on every tool call would drown the log."""
    global _warned_role_cap
    if _warned_role_cap:
        return
    _log.warning(
        "context carries %d distinct roles; evaluating the first %d "
        "(MAX_EVALUATED_ROLES). Later roles cannot grant access. "
        "Subsequent occurrences in this process are suppressed.",
        total,
        kept,
    )
    _warned_role_cap = True


def _roles_to_evaluate(context: HexgateContext | None) -> list[str | None]:
    """Adapt "no active context" to "no roles"; :func:`resolve_role_set` owns the
    normalisation, shared with the ``hexgate policy test`` dry-run."""
    return resolve_role_set(
        context.user_roles if context is not None else (),
        on_truncate=_warn_role_cap,
    )


# A sync, fire-and-forget hook fired after every decision is built.
# Used today by ``hexgate chat`` to render denies / approvals inline in the
# REPL; future consumers (metrics, debuggers) would slot in the same way.
# Distinct from ``AuditSender`` (which posts to the platform) — the observer
# stays on the caller's machine and gets the full ``Decision`` object, not
# a wire-shaped payload.
DecisionObserver = Callable[[Decision], None]


def _snapshot(values: Mapping[str, Any], *, deep: bool) -> dict[str, Any]:
    """Copy a mapping for retention on a :class:`Decision`.

    Deep when an audit sender or observer may inspect it after ``decide()``
    returns: a shallow copy would let the caller mutate a nested value first
    and make the captured record lie about what was decided.
    """
    return copy.deepcopy(dict(values)) if deep else dict(values)


def _usage_paths_of(engine: PolicyEngine) -> frozenset[str]:
    """The engine's ``agent_usage.*`` paths; none for an engine predating the method.
    Its ``evaluate`` must still accept the ``agent_usage`` keyword, as the protocol
    requires: :meth:`PolicyEnforcer.decide` always passes it."""
    paths = getattr(engine, "agent_usage_paths", None)
    return paths() if paths is not None else frozenset()


# The engine method 6b adds; until then every engine reads as allow (G3).
_ON_UNAVAILABLE_METHOD = "usage_on_unavailable"


def _on_unavailable_of(engine: PolicyEngine) -> OnUnavailable:
    read = getattr(engine, _ON_UNAVAILABLE_METHOD, None)
    return read() if read is not None else OnUnavailable.ALLOW


@dataclass(frozen=True, slots=True)
class _BoundPolicy:
    """An engine, its usage paths and its fail mode, published by one attribute
    write so a racing decision never sees a new path without the ledger that
    answers it."""

    engine: PolicyEngine
    usage_paths: frozenset[str]
    on_unavailable: OnUnavailable


@dataclass(frozen=True, slots=True)
class UsageRefresh:
    """One boundary's usage refresh: the source, the agent and the paths of the
    policy loaded when the run started (02 §5.5 Rule 2)."""

    source: PlatformUsageSource
    agent_name: str
    paths: frozenset[str]

    def run(self) -> None:
        self.source.refresh(self.agent_name, self.paths)

    async def arun(self) -> None:
        await self.source.refresh_async(self.agent_name, self.paths)


class PolicyEnforcer:
    """Evaluate proposed tool calls against a policy engine.

    ``policy`` is any :class:`~hexgate.security.decision.PolicyEngine` —
    in practice a :class:`~hexgate.security.policy_set.PolicySet` (the
    role-aware pydantic engine) or a
    :class:`~hexgate.security.bundle.PolicyBundle` (a compiled WASM bundle,
    the Rego enforcement path). The enforcer only knows the protocol, so
    it never branches on which engine ran.

    ``ledgers`` supplies the local term of the ``agent_usage.*`` namespace.
    Binding a policy that references a usage path enables it, on construction and
    on every swap. ``usage_source`` adds the platform term; ``None`` reads the
    ledger alone.
    """

    def __init__(
        self,
        policy: PolicyEngine,
        *,
        agent_name: str = "default",
        audit_sender: AuditSender | None = None,
        decision_observer: DecisionObserver | None = None,
        ledgers: UsageLedgers = AGENT_USAGE_LEDGERS,
        usage_source: PlatformUsageSource | None = None,
    ) -> None:
        self._ledgers = ledgers
        self._usage_source = usage_source
        self.policy = policy
        self.agent_name = agent_name
        # Injected per-agent so each agent emits with its own api_key's sender.
        # ``None`` means audit is inert for this enforcer.
        self._audit_sender = audit_sender
        # Local-process hook (no IO). ``hexgate chat`` injects one that
        # appends to a deque the REPL drains between turns; tests inject
        # a list-append. Distinct slot from audit so a deployment can have
        # one without the other.
        self._decision_observer = decision_observer

    @property
    def policy(self) -> PolicyEngine:
        return self._bound.engine

    @policy.setter
    def policy(self, engine: PolicyEngine) -> None:
        usage_paths = _usage_paths_of(engine)
        if usage_paths:
            # Before publishing: the first decision on the new policy must find a ledger.
            self._ledgers.enable()
        self._bound = _BoundPolicy(engine, usage_paths, _on_unavailable_of(engine))

    def usage_refresh(self) -> UsageRefresh | None:
        """The boundary's usage refresh, or None when there is nothing to fetch, so
        an agent without usage paths pays nothing at run start."""
        paths = self._bound.usage_paths
        if self._usage_source is None or not paths:
            return None
        return UsageRefresh(self._usage_source, self.agent_name, paths)

    def decide(self, tool_name: str, arguments: Mapping[str, Any]) -> Decision:
        """Fold one verdict per role from the active context into a
        :class:`Decision`. Access is granted iff any role grants it.

        Then emits an :class:`~hexgate.audit.AuditEvent` and calls
        ``decision_observer``; both no-op when not injected, and a broken
        observer never breaks enforcement."""
        context = get_current_context()
        roles = _roles_to_evaluate(context)
        # Feeds the ``ctx.*`` constraint namespace. Contextvar-sourced, so
        # spoofable at the same tier as the role set — not for
        # security-critical decisions until the signed tier verifies it.
        attributes = context.attributes if context is not None else None
        # Deep-copy only when something retains the Decision: the contextvar
        # outlives the call, so a shallow copy would let a later mutation
        # rewrite what the record says was decided. Taken once, then shared
        # across every role's evaluation.
        retained = self._audit_sender is not None or self._decision_observer is not None
        args_snapshot = _snapshot(arguments, deep=retained)
        attrs_snapshot = (
            _snapshot(attributes, deep=retained) if attributes is not None else None
        )

        # Feeds the ``run.*`` namespace. Read once, not per role: elapsed_seconds
        # moves and counters can change mid-fold, so N reads could let roles
        # disagree about the same run. No snapshot needed — this dict is
        # freshly built and held nowhere else.
        facts = get_run_facts()
        run_snapshot = facts.as_namespace(tool_name)
        bound = self._bound
        # Feeds the ``agent_usage.*`` namespace. Read once per decision, like run.*,
        # so roles can't disagree about the agent's usage. None when the policy
        # references none.
        reading = self._usage_reading(bound, facts, run_snapshot)
        usage_snapshot = reading.namespace if reading is not None else None

        verdict, deciding_role = combine_role_verdicts(
            roles,
            lambda role: bound.engine.evaluate(
                role=role,
                tool=tool_name,
                args=args_snapshot,
                attributes=attrs_snapshot,
                run=run_snapshot,
                agent_usage=usage_snapshot,
            ),
        )
        decision = Decision.from_verdict(
            verdict,
            agent_name=self.agent_name,
            tool_name=tool_name,
            user_roles=tuple(role for role in roles if role is not None),
            deciding_role=deciding_role,
            arguments=args_snapshot,
            attributes=attrs_snapshot,
            # The same snapshot the verdict saw, never a second read: the
            # record and the decision must not disagree about the run.
            run=RunAttribution.from_namespace(run_snapshot),
            usage_state=reading.state.value if reading is not None else None,
        )

        self.record(
            decision,
            user_id=context.user_id if context is not None and context.user_id else "",
            session_id=context.session_id
            if (context is not None and context.session_id)
            else "",
        )
        return decision

    def _usage_reading(
        self, bound: _BoundPolicy, facts: RunFacts, run: Mapping[str, Any]
    ) -> UsageReading | None:
        # By agent name, not RunFacts: admission is decided before run_scope opens,
        # while RunFacts is still DETACHED and carries no ledger.
        if not bound.usage_paths:
            return None
        ledger = self._ledgers.ledger_for(self.agent_name)
        if ledger is None:
            return None
        # Inside a run that recorded its invocation on this ledger (not a parent's,
        # not DETACHED, not one opened before the ledgers were enabled), so that
        # invocation is left out of invocations_*, as it was at admission.
        in_own_run = facts.ledger is ledger
        current_run_age = run[_RUN_ELAPSED] if in_own_run else None
        if self._usage_source is None:
            return local_reading(
                ledger, bound.usage_paths, current_run_age=current_run_age
            )
        return self._usage_source.read(
            self.agent_name,
            bound.usage_paths,
            ledger,
            on_unavailable=bound.on_unavailable,
            current_run_age=current_run_age,
        )

    def record(
        self, decision: Decision, *, user_id: str = "", session_id: str = ""
    ) -> None:
        """Emit ``decision`` to this enforcer's audit sender and decision
        observer, both isolated so a broken observer never breaks enforcement.

        ``decide`` calls this on every verdict; the guard runner calls it for a
        guard halt, which builds its own :class:`Decision` and so does not go
        through ``decide``. Without it a guard-blocked call would leave no trail
        (pre-halt) or record only the tool's ALLOW (post-halt)."""
        if self._audit_sender is not None:
            self._audit_sender.emit(
                AuditEvent(decision=decision, user_id=user_id, session_id=session_id)
            )
        if self._decision_observer is not None:
            try:
                self._decision_observer(decision)
            except Exception:
                # A broken observer (chat-panel render bug, third-party
                # subscriber raising) must not break enforcement — the
                # Decision the agent acts on is the source of truth.
                _log.exception("decision_observer raised; ignoring")


def build_enforcer(
    engine: PolicyEngine,
    *,
    agent_name: str = "default",
    api_key: str | None = None,
    decision_observer: DecisionObserver | None = None,
) -> PolicyEnforcer:
    """Compose a governed enforcer — engine + audit sender + usage source from
    ``api_key``.

    The one place that pairs an engine with its audit sink and usage source, so
    the six surfaces (``HexgateAgent.enforce_policy``, the four adapters, the
    OpenAI runner) don't each repeat the wiring. ``api_key=None`` falls back to
    ``HEXGATE_API_KEY`` (audit stays inert and usage stays local when neither
    resolves). ``decision_observer`` threads the local-process decision hook
    (see :class:`PolicyEnforcer`); ``None`` is silent.
    """
    return PolicyEnforcer(
        engine,
        agent_name=agent_name,
        audit_sender=configure(api_key),
        decision_observer=decision_observer,
        usage_source=resolve_usage_source(api_key=api_key),
    )
