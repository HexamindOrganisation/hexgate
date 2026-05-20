"""Tool-shape-agnostic policy enforcement.

:class:`PolicyEnforcer` is the single entry point for evaluating a proposed
tool call against a :class:`~fortify.security.policy_set.PolicySet`. It is
stateless across calls: each :meth:`PolicyEnforcer.decide` re-reads the
active :class:`~fortify.runtime.User` from the contextvar so the same
enforcer instance can serve callers in different role scopes.

The enforcer never executes a tool. It returns a
:class:`~fortify.security.decision.Decision` and stops. Adapters
(LangChain, OpenAI, MCP, plain callables) sit on top of this and translate
the decision into whatever their host expects.
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

    def __init__(
        self, policy_set: PolicySet, *, agent_name: str | None = None
    ) -> None:
        self.policy_set = policy_set
        self.agent_name = agent_name

    def decide(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> Decision:
        """Run the enforcement pipeline and return a typed Decision.

        Resolves the active role from the contextvar, selects the matching
        :class:`~fortify.security.models.AgentPolicy` from the bundle, then
        runs the deterministic mode/constraints/file-scope checks via
        :func:`~fortify.security.policy.authorize_tool_call`.
        """
        user = get_current_user()
        role = user.role if user is not None else None
        policy = self.policy_set.policy_for(role)

        try:
            authorize_tool_call(policy, tool_name, dict(arguments))
        except PolicyDeniedError as exc:
            return Decision(
                outcome=DecisionOutcome.DENY,
                tool_name=tool_name,
                role=role,
                reason=str(exc),
                error_type="policy_denied",
                hint=_hint_for(policy, tool_name),
            )
        except ApprovalRequiredError as exc:
            return Decision(
                outcome=DecisionOutcome.NEEDS_APPROVAL,
                tool_name=tool_name,
                role=role,
                reason=str(exc),
                error_type="approval_required",
            )

        return Decision(
            outcome=DecisionOutcome.ALLOW,
            tool_name=tool_name,
            role=role,
        )


def _hint_for(policy: AgentPolicy, tool_name: str) -> dict[str, Any] | None:
    """Return a machine-readable hint when a denial is path-shaped."""
    tool_policy = policy.tools.get(tool_name)
    if isinstance(tool_policy, FileToolPolicy):
        return build_file_scope_hint(tool_policy)
    return None
