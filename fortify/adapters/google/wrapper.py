"""Google ADK adapter: resolve the platform policy, construct one
:class:`PolicyEnforcer`, and return a clone of the agent whose tools
are policy-gated. User-agnostic at wrap time — role resolution happens
inside the enforcer via the :class:`User` contextvar.

Policy comes from the platform (policy-binding spec, phase 5):
wrap-time :meth:`~fortify.security.binding.PolicyBinding.resolve` pulls
+ verifies the current policy (``FORTIFY_LOCAL_POLICY`` override →
signed bundle → pydantic fallback), an unknown agent is registered from
its in-code definition and resolved again. The returned binding is what
the :class:`~fortify.adapters.google.runner.FortifyRunner` refreshes at
the top of every run — the clone and its gated tools never change; only
``enforcer.policy`` swaps.
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

    An unknown agent exists only in code so far: register it from its own
    definition (ADK agents are introspectable — name, model, instruction,
    tools all come off the object) and resolve again; the platform
    answers a first register with a default role-aware policy + signed
    bundle. Anything else (bad key, bad signature, platform down) stays
    loud — wrapping asked for governance, so failing to bind is an
    error, never a silently allow-all agent.
    """
    from fortify.cloud.client import FortifyError

    try:
        return PolicyBinding.resolve(agent_name, api_key=api_key)
    except FortifyError as exc:
        if exc.status != 404:
            raise
        from fortify.cli.register import register_agent

        logger.info(
            "agent %r not registered on the platform — registering it from "
            "the in-code ADK definition",
            agent_name,
        )
        register_agent(agent)
        return PolicyBinding.resolve(agent_name, api_key=api_key)


def wrap_google_agent(
    agent: BaseAgent, *, api_key: str
) -> tuple[BaseAgent, PolicyBinding]:
    """Return a policy-gated clone of ``agent`` plus its refresh binding.

    Caller must open a :class:`User` scope around the run.
    ``NEEDS_APPROVAL`` outcomes surface as ``[approval_required]``-prefixed
    strings in tool results; ``[policy_denied]`` for denials.

    The enforced policy is the platform's (see :func:`_resolve_binding`);
    tools not named in that policy are denied-by-absence at call time.
    Hold on to the returned binding and call
    :meth:`~fortify.security.binding.PolicyBinding.refresh` at your run
    boundaries — :class:`~fortify.adapters.google.runner.FortifyRunner`
    does this for you.
    """
    agent_name = getattr(agent, "name", "default")
    tools = list(getattr(agent, "tools", []) or [])

    binding = _resolve_binding(agent, agent_name, api_key)
    # Rebuild the enforcer around the resolved engine so the adapter's
    # audit sender rides along — resolve() is adapter-agnostic and doesn't
    # know about audit. The rebound binding keeps the seeded source, so
    # refresh still swaps THIS enforcer's policy in place.
    enforcer = PolicyEnforcer(
        binding.enforcer.policy,
        agent_name=agent_name,
        audit_sender=audit.configure(api_key),
    )
    binding = PolicyBinding(enforcer, binding.source)

    guarded_tools = wrap_tools(tools, enforcer)
    return agent.model_copy(update={"tools": guarded_tools}), binding
