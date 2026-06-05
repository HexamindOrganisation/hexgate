"""OpenAI Agents adapter: resolve the platform policy and return a clone
of the agent whose tools are policy-gated. User-agnostic at wrap time —
role resolution happens inside the enforcer via the :class:`User`
contextvar.

Policy is resolved from the platform (register-on-404); the lifecycle —
binding cache + per-run refresh — lives in the runner, since the OpenAI
``Runner`` receives the agent per call.
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


def wrap_openai_agent(agent: Agent, *, enforcer: PolicyEnforcer) -> Agent:
    """Return a clone of ``agent`` whose tools are gated by ``enforcer``.

    Mechanics only — resolution/refresh live with the caller. Caller
    must open a :class:`User` scope around the run.
    """
    guarded_tools = wrap_tools(agent.tools, enforcer)
    return dataclasses.replace(agent, tools=guarded_tools)
