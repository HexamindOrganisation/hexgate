"""The OpenAI Agents SDK agent the bridge serves to watsonx Orchestrate.

Serves the stub DevOps tools from ``examples/devops_openai.py`` under its own
name, so it registers as a separate agent. Paste ``examples/devops_policy.yaml`` into the
platform policy editor for ``orchestrate_devops_agent``.

Register it once before serving (``HexgateRunner`` fails loud on an unregistered
agent)::

    hexgate register --agent examples.watsonx_orchestrate.agent:agent
"""

from __future__ import annotations

import os

from agents import Agent

# Reuse the DevOps stub tools so examples/devops_policy.yaml matches both agents.
from examples.devops_openai import read_logs, restart_service, scale_deployment

INSTRUCTION = (
    "You are a DevOps assistant for a Kubernetes platform. Help engineers read "
    "service logs, restart services, and scale deployments. Pull the service "
    "name, replica count, and environment (dev/staging/prod) from the request "
    "and act directly — the policy layer gates sensitive actions. When a tool "
    "returns a [policy_denied] or [approval_required] marker, tell the user the "
    "action was blocked by policy and why."
)


agent = Agent(
    name="orchestrate_devops_agent",
    instructions=INSTRUCTION,
    tools=[read_logs, restart_service, scale_deployment],
    model=os.environ.get("HEXGATE_ORCHESTRATE_MODEL", "gpt-4o-mini"),
)
