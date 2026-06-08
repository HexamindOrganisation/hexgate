"""Google ADK adapter: resolve the platform policy, construct one
:class:`PolicyEnforcer`, and return a clone of the agent whose tools
are policy-gated. User-agnostic at wrap time — role resolution happens
inside the enforcer via the :class:`User` contextvar.

Policy is resolved from the platform at wrap time (register-on-404);
the returned binding is what the runner refreshes per run.
"""

from __future__ import annotations

from google.adk.agents import BaseAgent

from fortify.adapters.google.tools import wrap_tools
from fortify.security.binding import PolicyBinding, resolve_policy_or_register
from fortify.security.enforcer import build_enforcer


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

    def _register() -> None:
        from fortify.cli.register import register_agent

        register_agent(agent)

    resolved = resolve_policy_or_register(
        agent_name, api_key=api_key, on_missing=_register
    )
    enforcer = build_enforcer(resolved.engine, agent_name=agent_name, api_key=api_key)
    guarded_tools = wrap_tools(tools, enforcer)
    return (
        agent.model_copy(update={"tools": guarded_tools}),
        PolicyBinding(enforcer, resolved.source),
    )
