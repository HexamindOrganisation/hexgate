"""Top-level shim for applying policy enforcement to a Fortify agent.

The heavy lifting moved into :class:`fortify.security.enforcer.PolicyEnforcer`
and :class:`fortify.adapters.langchain.tools.GuardedTool`. This module
keeps a single convenience function — :func:`enforce_policy` — that
delegates to :meth:`FortifyAgent.enforce_policy` so older call sites
(loaders, examples, top-level ``fortify`` imports) don't have to know
about the adapter layout.
"""

from __future__ import annotations

from typing import Any

from fortify.agents.factory import AgentGraph
from fortify.security import AgentPolicy, PolicySet


def enforce_policy(
    agent: AgentGraph,
    policy: str | AgentPolicy | PolicySet | None,
    *,
    approval_handler: Any = None,
) -> AgentGraph:
    """Return an agent runtime with policy enforcement applied.

    ``policy`` may be a path to a single YAML file (legacy), a directory of
    role policies (the new shape), an :class:`AgentPolicy` model, or a
    pre-built :class:`PolicySet`.

    ``approval_handler`` resolves ``NEEDS_APPROVAL`` outcomes inline at the
    adapter level — see :meth:`FortifyAgent.enforce_policy` for the contract.
    """
    return agent.enforce_policy(policy, approval_handler=approval_handler)
