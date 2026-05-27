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
from fortify.security.bundle import PolicyBundle
from fortify.security.decision import Decision, DecisionOutcome
from fortify.security.errors import ApprovalRequiredError, PolicyDeniedError
from fortify.security.file_scope import build_file_scope_hint
from fortify.security.models import AgentPolicy, FileToolPolicy
from fortify.security.policy import authorize_tool_call, authorize_tool_call_wasm
from fortify.security.policy_set import PolicySet


_DEFAULT_ROLE = "default"


class PolicyEnforcer:
    """Evaluate proposed tool calls against a policy.

    ``policy`` is one of two interchangeable engines:

      * a :class:`PolicySet` — the role-aware pydantic engine; or
      * a :class:`PolicyBundle` — a compiled WASM bundle (the Rego
        enforcement path served by the platform or built from a
        ``policies/`` dir).

    They're two implementations of one concept, so a single slot (rather
    than two optional ones) makes "exactly one engine" a type-level
    guarantee instead of a runtime check. Either way :meth:`decide`
    returns the same :class:`Decision`, so adapters (e.g.
    :class:`~fortify.adapters.langchain.tools.GuardedTool`) never need to
    know which engine ran.
    """

    def __init__(
        self,
        policy: PolicySet | PolicyBundle,
        *,
        agent_name: str = "default",
    ) -> None:
        self.policy = policy
        self.agent_name = agent_name

    def decide(self, tool_name: str, arguments: Mapping[str, Any]) -> Decision:
        """Resolve role from the contextvar, run mode/constraints/file-scope
        checks via the active engine, return a :class:`Decision`."""
        user = get_current_user()
        role = user.role if user is not None else None
        args_snapshot = dict(arguments)

        # The WASM engine produces no pydantic policy object, so no file-scope
        # hint — its denial messages already embed the raw constraint violations.
        policy: AgentPolicy | None = None

        try:
            if isinstance(self.policy, PolicyBundle):
                authorize_tool_call_wasm(
                    self.policy,
                    role or _DEFAULT_ROLE,
                    tool_name,
                    args_snapshot,
                )
            else:
                policy = self.policy.policy_for(role)
                authorize_tool_call(policy, tool_name, args_snapshot)
        except PolicyDeniedError as exc:
            return Decision(
                outcome=DecisionOutcome.DENY,
                agent_name=self.agent_name,
                tool_name=tool_name,
                role=role,
                reason=str(exc),
                error_type="policy_denied",
                hint=_hint_for(policy, tool_name) if policy is not None else None,
                arguments=args_snapshot,
            )
        except ApprovalRequiredError as exc:
            return Decision(
                outcome=DecisionOutcome.NEEDS_APPROVAL,
                agent_name=self.agent_name,
                tool_name=tool_name,
                role=role,
                reason=str(exc),
                error_type="approval_required",
                arguments=args_snapshot,
            )

        return Decision(
            outcome=DecisionOutcome.ALLOW,
            agent_name=self.agent_name,
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
