"""OpenAI Agents adapter: resolve the platform policy and return a clone
of the agent whose tools are policy-gated. HexgateContext-agnostic at wrap time —
role resolution happens inside the enforcer via the :class:`HexgateContext`
contextvar.

Policy is resolved from the platform (register-on-404); the lifecycle —
binding cache + per-run refresh — lives in the runner, since the OpenAI
``Runner`` receives the agent per call.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import TYPE_CHECKING

from agents import Agent

from hexgate.adapters.openai.tools import wrap_tools
from hexgate.approvals import ApprovalHandler
from hexgate.guards.types import build_pipeline
from hexgate.security.enforcer import PolicyEnforcer

if TYPE_CHECKING:
    from hexgate.guards.types import Guard, GuardObserver


def wrap_openai_agent(
    agent: Agent,
    *,
    enforcer: PolicyEnforcer,
    approval_handler: ApprovalHandler | None = None,
    guards: Sequence[Guard] | None = None,
    guard_observer: GuardObserver | None = None,
) -> Agent:
    """Return a clone of ``agent`` whose tools are gated by ``enforcer``.

    Mechanics only — resolution/refresh live with the caller. Caller
    must open a :class:`HexgateContext` scope around the run. ``approval_handler``
    (async ``fn(decision) -> bool`` or ``bool`` shorthand) fires when a
    tool call carries a ``NEEDS_APPROVAL`` outcome; a truthy return runs
    the tool, falsy surfaces the ``[approval_required]`` marker. ``guards`` is the
    flat ``@before_tool`` / ``@after_tool`` guard list run around each tool call
    (the same argument the other adapters take); ``guard_observer`` receives the
    provenance ``GuardEvent``s.
    """
    # The closed-world check of the policy's guard stance is NOT run here: this
    # wrapper is called on every run (after a per-run refresh), and R-GUARD-007
    # requires a refresh that swaps in a policy naming an unknown guard to degrade
    # to a no-op, not crash the running agent. The runner validates once, when the
    # binding is first resolved (see ``HexgateRunner._binding_for``) — that is the
    # supported entry point. A caller that drives this mechanics-only wrapper itself
    # (outside HexgateRunner) owns that fail-fast, exactly as it owns resolution and
    # refresh. The enable/disable stance is applied per call in the guard runner, so
    # one shared pipeline is installed on every tool.
    pipeline = build_pipeline(guards, observer=guard_observer)
    guarded_tools = wrap_tools(
        agent.tools, enforcer, approval_handler=approval_handler, pipeline=pipeline
    )
    return dataclasses.replace(agent, tools=guarded_tools)
