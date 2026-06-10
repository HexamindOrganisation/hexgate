"""Tool-shape-agnostic policy enforcement.

:class:`PolicyEnforcer` returns a :class:`Decision` for a proposed tool
call and stops — adapters translate it for their host. Stateless across
calls: each :meth:`decide` re-reads the active :class:`User` from the
contextvar.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from hexgate.audit import AuditEvent, AuditSender, configure
from hexgate.runtime.context import get_current_user
from hexgate.security.decision import Decision, PolicyEngine


class PolicyEnforcer:
    """Evaluate proposed tool calls against a policy engine.

    ``policy`` is any :class:`~hexgate.security.decision.PolicyEngine` —
    in practice a :class:`~hexgate.security.policy_set.PolicySet` (the
    role-aware pydantic engine) or a
    :class:`~hexgate.security.bundle.PolicyBundle` (a compiled WASM bundle,
    the Rego enforcement path). The enforcer only knows the protocol, so
    it never branches on which engine ran.
    """

    def __init__(
        self,
        policy: PolicyEngine,
        *,
        agent_name: str = "default",
        audit_sender: AuditSender | None = None,
    ) -> None:
        self.policy = policy
        self.agent_name = agent_name
        # Injected per-agent so each agent emits with its own api_key's sender.
        # ``None`` means audit is inert for this enforcer.
        self._audit_sender = audit_sender

    def decide(self, tool_name: str, arguments: Mapping[str, Any]) -> Decision:
        """Resolve role from the contextvar, ask the engine for a
        :class:`~hexgate.security.decision.Verdict`, and lift it into a
        host-facing :class:`Decision` with this agent's context.

        Emits an :class:`~hexgate.audit.AuditEvent` to this enforcer's
        injected sender after the decision is built. No-op when no sender
        was injected."""
        user = get_current_user()
        role = user.role if user is not None else None
        # Deep-copy when auditing: emission is async, so a shallow copy
        # would let the caller mutate nested args before the payload is
        # serialized, making the audit record lie about what was decided.
        args_snapshot = (
            copy.deepcopy(dict(arguments))
            if self._audit_sender is not None
            else dict(arguments)
        )

        verdict = self.policy.evaluate(role=role, tool=tool_name, args=args_snapshot)
        decision = Decision.from_verdict(
            verdict,
            agent_name=self.agent_name,
            tool_name=tool_name,
            role=role,
            arguments=args_snapshot,
        )

        if self._audit_sender is not None:
            self._audit_sender.emit(
                AuditEvent(
                    decision=decision,
                    user_id=user.user_id if user is not None else "",
                    session_id=user.session_id
                    if (user is not None and user.session_id)
                    else "",
                )
            )

        return decision


def build_enforcer(
    engine: PolicyEngine,
    *,
    agent_name: str = "default",
    api_key: str | None = None,
) -> PolicyEnforcer:
    """Compose a governed enforcer — engine + audit sender from ``api_key``.

    The one place that pairs an engine with its audit sink, so the six
    surfaces (``HexgateAgent.enforce_policy``, the four adapters, the
    OpenAI runner) don't each repeat the ``audit.configure`` wiring.
    ``api_key=None`` falls back to ``HEXGATE_KEY`` (audit stays inert when
    neither resolves).
    """
    return PolicyEnforcer(
        engine, agent_name=agent_name, audit_sender=configure(api_key)
    )
