"""Tool-shape-agnostic policy enforcement.

:class:`PolicyEnforcer` returns a :class:`Decision` for a proposed tool
call and stops — adapters translate it for their host. Stateless across
calls: each :meth:`decide` re-reads the active :class:`User` from the
contextvar.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fortify.audit import AuditEvent, get_sink
from fortify.runtime.context import get_current_user
from fortify.security.decision import Decision, PolicyEngine


class PolicyEnforcer:
    """Evaluate proposed tool calls against a policy engine.

    ``policy`` is any :class:`~fortify.security.decision.PolicyEngine` —
    in practice a :class:`~fortify.security.policy_set.PolicySet` (the
    role-aware pydantic engine) or a
    :class:`~fortify.security.bundle.PolicyBundle` (a compiled WASM bundle,
    the Rego enforcement path). The enforcer only knows the protocol, so
    it never branches on which engine ran.
    """

    def __init__(
        self,
        policy: PolicyEngine,
        *,
        agent_name: str = "default",
    ) -> None:
        self.policy = policy
        self.agent_name = agent_name

    def decide(self, tool_name: str, arguments: Mapping[str, Any]) -> Decision:
        """Resolve role from the contextvar, ask the engine for a
        :class:`~fortify.security.decision.Verdict`, and lift it into a
        host-facing :class:`Decision` with this agent's context.

        Emits an :class:`~fortify.audit.AuditEvent` to the configured sink
        after the decision is built. No-op when no sink is configured."""
        user = get_current_user()
        role = user.role if user is not None else None
        args_snapshot = dict(arguments)

        verdict = self.policy.evaluate(
            role=role, tool=tool_name, args=args_snapshot
        )
        decision = Decision.from_verdict(
            verdict,
            agent_name=self.agent_name,
            tool_name=tool_name,
            role=role,
            arguments=args_snapshot,
        )

        sink = get_sink()
        if sink is not None:
            sink.emit(AuditEvent(
                decision=decision,
                user_id=user.user_id if user is not None else "",
                session_id=user.session_id if (user is not None and user.session_id) else "",
            ))

        return decision
