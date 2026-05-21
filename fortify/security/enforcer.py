"""Tool-shape-agnostic policy enforcement.

:class:`PolicyEnforcer` returns a :class:`Decision` for a proposed tool
call and stops — adapters translate it for their host. Stateless across
calls: each :meth:`decide` re-reads the active :class:`User` from the
contextvar.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fortify.runtime.context import get_current_user
from fortify.security.decision import Decision, DecisionOutcome
from fortify.security.errors import ApprovalRequiredError, PolicyDeniedError
from fortify.security.file_scope import build_file_scope_hint
from fortify.security.models import AgentPolicy, FileToolPolicy
from fortify.security.policy import authorize_tool_call
from fortify.security.policy_set import PolicySet


class PolicyEnforcer:
    """Evaluate proposed tool calls against a role-aware PolicySet."""

    def __init__(self, policy_set: PolicySet, *, agent_name: str | None = None) -> None:
        self.policy_set = policy_set
        self.agent_name = agent_name

    def decide(self, tool_name: str, arguments: Mapping[str, Any]) -> Decision:
        """Resolve role from the contextvar, run mode/constraints/file-scope
        checks via :func:`authorize_tool_call`, return a :class:`Decision`."""
        user = get_current_user()
        role = user.role if user is not None else None
        policy = self.policy_set.policy_for(role)
        args_snapshot = dict(arguments)

        try:
            authorize_tool_call(policy, tool_name, args_snapshot)
        except PolicyDeniedError as exc:
            return Decision(
                outcome=DecisionOutcome.DENY,
                tool_name=tool_name,
                role=role,
                reason=str(exc),
                error_type="policy_denied",
                hint=_hint_for(policy, tool_name),
                arguments=args_snapshot,
            )
        except ApprovalRequiredError as exc:
            return Decision(
                outcome=DecisionOutcome.NEEDS_APPROVAL,
                tool_name=tool_name,
                role=role,
                reason=str(exc),
                error_type="approval_required",
                arguments=args_snapshot,
            )

        return Decision(
            outcome=DecisionOutcome.ALLOW,
            tool_name=tool_name,
            role=role,
            arguments=args_snapshot,
        )


def _hint_for(policy: AgentPolicy, tool_name: str) -> dict[str, Any] | None:
    """Return a machine-readable hint when the denial is path-shaped."""
    tool_policy = policy.tools.get(tool_name)
    if isinstance(tool_policy, FileToolPolicy):
        return build_file_scope_hint(tool_policy)
    return None
