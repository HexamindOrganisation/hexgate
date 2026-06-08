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

from agents import Agent

from fortify.adapters.openai.tools import wrap_tools
from fortify.security.enforcer import PolicyEnforcer


def wrap_openai_agent(agent: Agent, *, enforcer: PolicyEnforcer) -> Agent:
    """Return a clone of ``agent`` whose tools are gated by ``enforcer``.

    Mechanics only — resolution/refresh live with the caller. Caller
    must open a :class:`User` scope around the run.
    """
    guarded_tools = wrap_tools(agent.tools, enforcer)
    return dataclasses.replace(agent, tools=guarded_tools)
