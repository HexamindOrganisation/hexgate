"""Google ADK adapter: resolve the platform policy, construct one
:class:`PolicyEnforcer`, and return a clone of the agent whose tools
are policy-gated. User-agnostic at wrap time — role resolution happens
inside the enforcer via the :class:`User` contextvar.

Policy is resolved from the platform at wrap time (register-on-404);
the returned binding is what the runner refreshes per run.
"""

from __future__ import annotations

import logging

from google.adk.agents import BaseAgent

from fortify import audit
from fortify.adapters.google.tools import wrap_tools
from fortify.security.binding import PolicyBinding
from fortify.security.enforcer import PolicyEnforcer

logger = logging.getLogger("fortify.adapters.google")


def _resolve_binding(
    agent: BaseAgent, agent_name: str, api_key: str
) -> PolicyBinding:
    """Resolve the platform policy for ``agent_name``, registering on 404.

    Non-404 failures stay loud — never a silent allow-all.
    """
    from fortify.cloud.client import FortifyError

    try:
        return PolicyBinding.resolve(agent_name, api_key=api_key)
    except FortifyError as exc:
        if exc.status != 404:
            raise
        from fortify.cli.register import register_agent

        logger.info("agent %r not registered — registering it from code", agent_name)
        register_agent(agent)
        return PolicyBinding.resolve(agent_name, api_key=api_key)


def wrap_google_agent(
    agent: BaseAgent, *, api_key: str
) -> tuple[BaseAgent, PolicyBinding]:
    """Return a policy-gated clone of ``agent`` plus its refresh binding.

    Caller must open a :class:`User` scope around the run.
    ``NEEDS_APPROVAL`` outcomes surface as ``[approval_required]``-prefixed
    strings in tool results; ``[policy_denied]`` for denials. Refresh the
    returned binding at run boundaries (``FortifyRunner`` does).
    """
    agent_name = getattr(agent, "name", "default")
    tools = list(getattr(agent, "tools", []) or [])

    binding = _resolve_binding(agent, agent_name, api_key)
    # Rebuild the enforcer to inject the audit sender; the rebound binding
    # keeps the seeded source, so refresh swaps this enforcer in place.
    enforcer = PolicyEnforcer(
        binding.enforcer.policy,
        agent_name=agent_name,
        audit_sender=audit.configure(api_key),
    )
    binding = PolicyBinding(enforcer, binding.source)

    guarded_tools = wrap_tools(tools, enforcer)
    return agent.model_copy(update={"tools": guarded_tools}), binding
