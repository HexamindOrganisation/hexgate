"""OpenAI Agents adapter: resolve the platform policy and return a clone
of the agent whose tools are policy-gated. User-agnostic at wrap time —
role resolution happens inside the enforcer via the :class:`User`
contextvar.

Policy comes from the platform (policy-binding spec, phase 6):
:func:`_resolve_binding` pulls + verifies the current policy
(``FORTIFY_LOCAL_POLICY`` override → signed bundle → pydantic fallback),
registering an unknown agent from its in-code definition. Because the
OpenAI ``Runner`` receives the agent per call, the lifecycle lives in
:class:`~fortify.adapters.openai.runner.FortifyRunner`: it caches one
binding per agent name (preserving the ETag memory across calls),
refreshes it at the top of every run, and re-wraps the agent with the
cached enforcer — cheap ``copy.copy`` of the tools; the enforcer they
close over is the shared one a refresh swaps.
"""

from __future__ import annotations

import dataclasses
import logging

from agents import Agent

from fortify.adapters.openai.tools import wrap_tools
from fortify.security.binding import PolicyBinding
from fortify.security.enforcer import PolicyEnforcer

logger = logging.getLogger("fortify.adapters.openai")


def _resolve_binding(agent: Agent, agent_name: str, api_key: str) -> PolicyBinding:
    """Resolve the platform policy for ``agent_name``, registering on 404.

    An unknown agent exists only in code so far: register it from its own
    definition (OpenAI ``Agent`` objects are introspectable — name, model,
    instructions, tools all come off the object) and resolve again; the
    platform answers a first register with a default role-aware policy +
    signed bundle. Anything else (bad key, bad signature, platform down)
    stays loud — running asked for governance, so failing to bind is an
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
            "the in-code OpenAI Agents definition",
            agent_name,
        )
        register_agent(agent)
        return PolicyBinding.resolve(agent_name, api_key=api_key)


def wrap_openai_agent(agent: Agent, *, enforcer: PolicyEnforcer) -> Agent:
    """Return a clone of ``agent`` whose tools are gated by ``enforcer``.

    Pure mechanics — policy resolution and refresh live with the caller
    (see :class:`~fortify.adapters.openai.runner.FortifyRunner`). The
    clone's tool copies close over ``enforcer``, so re-wrapping per call
    is cheap and a refresh that rebinds ``enforcer.policy`` reaches every
    previously produced clone. Caller must open a :class:`User` scope
    around the run — role/constraints resolve at call time from the
    contextvar.
    """
    guarded_tools = wrap_tools(agent.tools, enforcer)
    return dataclasses.replace(agent, tools=guarded_tools)
