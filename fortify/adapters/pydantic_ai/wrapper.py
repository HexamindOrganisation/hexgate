"""pydantic_ai adapter: resolve the platform policy, construct one
:class:`PolicyEnforcer`, and return a :class:`FortifyPydanticAgent`
proxy backed by a clone of the caller's ``Agent`` with policy-gated
tools.

Policy comes from the platform (policy-binding spec, phase 7): wrap-time
:meth:`~fortify.security.binding.PolicyBinding.resolve` pulls + verifies
the current policy (``FORTIFY_LOCAL_POLICY`` override → signed bundle →
pydantic fallback), an unknown agent is registered from its in-code
definition and resolved again, and the proxy refreshes the binding at
the top of every run — so dashboard edits land at the next run with a
cheap ETag/304 round trip.
"""

from __future__ import annotations

import copy
import logging
import os

from pydantic_ai import Agent
from pydantic_ai.tools import Tool

from fortify import audit
from fortify.adapters.pydantic_ai.agent import FortifyPydanticAgent
from fortify.adapters.pydantic_ai.tools import wrap_tools
from fortify.security.binding import PolicyBinding
from fortify.security.enforcer import PolicyEnforcer

logger = logging.getLogger("fortify.adapters.pydantic_ai")


def _extract_tools(agent: Agent) -> list[Tool]:
    """Return Tool instances from ``agent._function_toolset`` (constructor
    args and ``@agent.tool``/``tool_plain`` decorators normalize there)."""
    toolset = getattr(agent, "_function_toolset", None)
    tools = getattr(toolset, "tools", None) if toolset is not None else None
    if tools is None:
        return []
    return list(tools.values())


def _clone_agent_with_tools(agent: Agent, wrapped_tools: list[Tool]) -> Agent:
    """Return a shallow copy of ``agent`` with ``wrapped_tools`` installed."""
    agent_copy = copy.copy(agent)
    agent_copy.instrument = True
    toolset = getattr(agent, "_function_toolset", None)
    if toolset is not None:
        toolset_copy = copy.copy(toolset)
        toolset_copy.tools = {t.name: t for t in wrapped_tools}
        agent_copy._function_toolset = toolset_copy
    return agent_copy


def _resolve_binding(agent: Agent, agent_name: str, api_key: str) -> PolicyBinding:
    """Resolve the platform policy for ``agent_name``, registering on 404.

    An unknown agent exists only in code so far: register it from its own
    definition (pydantic_ai agents are introspectable — name, model,
    prompt, toolset all come off the object) and resolve again; the
    platform answers a first register with a default role-aware policy +
    signed bundle. Anything else (bad key, bad signature, platform down)
    stays loud — wrapping asked for governance, so failing to bind is an
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
            "the in-code pydantic_ai definition",
            agent_name,
        )
        register_agent(agent)
        return PolicyBinding.resolve(agent_name, api_key=api_key)


def wrap_pydantic_agent(
    *,
    agent: Agent,
    api_key: str | None = None,
) -> FortifyPydanticAgent:
    """Wrap a pydantic_ai agent with Fortify policy + observability.

    Returns a :class:`FortifyPydanticAgent` backed by a clone of the
    caller's ``agent``; the original is not mutated. The proxy takes
    ``user`` per call; role resolves at call time from the active
    :class:`User`. ``NEEDS_APPROVAL`` raises :class:`ModelRetry` with
    an ``[approval_required]`` marker. ``api_key`` falls back to
    ``FORTIFY_KEY``.

    The enforced policy is the platform's (see :func:`_resolve_binding`),
    and the proxy re-pulls it at the top of every run. Tools not named
    in that policy are denied-by-absence at call time.
    """
    resolved_key = api_key or os.getenv("FORTIFY_KEY")
    if not resolved_key:
        raise ValueError(
            "No API key provided. Pass api_key= explicitly or set FORTIFY_KEY environment variable."
        )

    agent_name = getattr(agent, "name", None) or "default"
    tools = _extract_tools(agent)

    binding = _resolve_binding(agent, agent_name, resolved_key)
    # Rebuild the enforcer around the resolved engine so the adapter's
    # audit sender rides along — resolve() is adapter-agnostic and doesn't
    # know about audit. The rebound binding keeps the seeded source, so
    # refresh still swaps THIS enforcer's policy in place.
    enforcer = PolicyEnforcer(
        binding.enforcer.policy,
        agent_name=agent_name,
        audit_sender=audit.configure(resolved_key),
    )
    binding = PolicyBinding(enforcer, binding.source)

    wrapped_tools = wrap_tools(tools, enforcer)
    cloned_agent = _clone_agent_with_tools(agent, wrapped_tools)

    return FortifyPydanticAgent(
        agent=cloned_agent,
        api_key=resolved_key,
        agent_name=agent_name,
        binding=binding,
    )
