"""Helpers for loading and evaluating agent security policies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from fortify.security.errors import ApprovalRequiredError, PolicyDeniedError
from fortify.security.file_scope import is_path_allowed
from fortify.security.models import AgentPolicy, FileToolPolicy, ToolPolicy
from fortify.security.predicates import (
    FactDict,
    check_numeric_limit,
    check_requires_scope,
    check_requires_user,
)


def default_agent_policy() -> AgentPolicy:
    """Return the default deny-by-default policy."""
    return AgentPolicy()


def load_policy(policy: str | Path | AgentPolicy | None) -> AgentPolicy:
    """Load and validate an agent policy from YAML or an existing model."""
    if policy is None:
        return default_agent_policy()
    if isinstance(policy, AgentPolicy):
        return policy

    path = Path(policy)
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return AgentPolicy.model_validate(payload)


def get_tool_policy(policy: AgentPolicy, tool_name: str) -> ToolPolicy:
    """Resolve the effective policy for a tool name."""
    return policy.tools.get(tool_name, policy.default_policy)


def authorize_tool_call(
    policy: AgentPolicy,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    facts: FactDict | None = None,
) -> None:
    """Raise when a tool call is denied or requires approval.

    ``facts`` is the Biscuit-extracted single-arity dict (see
    :func:`fortify.cloud.biscuit.extract_facts`). When the tool policy
    declares any of ``requires_user`` / ``requires_scope`` /
    ``numeric_limit``, the matching predicate is evaluated against the
    facts here — fail-closed if a requirement exists and the token
    doesn't carry the corresponding fact.
    """
    tool_policy = get_tool_policy(policy, tool_name)
    if tool_policy.mode == "deny":
        raise PolicyDeniedError(f'Policy denied tool "{tool_name}"')

    check_requires_user(tool_policy.requires_user, facts, tool_name)
    check_requires_scope(tool_policy.requires_scope, facts, tool_name)
    check_numeric_limit(tool_policy.numeric_limit, facts, arguments, tool_name)

    if isinstance(tool_policy, FileToolPolicy) and not is_path_allowed(
        tool_name, arguments, tool_policy
    ):
        raise PolicyDeniedError(
            f'Policy denied tool "{tool_name}" for the requested path'
        )

    if tool_policy.mode == "allow":
        return
    if tool_policy.mode == "approval_required":
        raise ApprovalRequiredError(f'Policy requires approval for tool "{tool_name}"')
    raise PolicyDeniedError(f'Policy denied tool "{tool_name}"')
