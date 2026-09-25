"""The OpenAI Agents SDK agent the bridge serves to watsonx Orchestrate.

Same stub DevOps tools as ``examples/devops_openai.py``, under its own name so it
registers as a separate agent. Paste ``examples/devops_policy.yaml`` into the
platform policy editor for ``orchestrate_devops_agent``.

Register it once before serving (``HexgateRunner`` fails loud on an unregistered
agent)::

    hexgate register --agent examples.watsonx_orchestrate.agent:agent
"""

from __future__ import annotations

import os

from agents import Agent, function_tool

INSTRUCTION = (
    "You are a DevOps assistant for a Kubernetes platform. Help engineers read "
    "service logs, restart services, and scale deployments. Pull the service "
    "name, replica count, and environment (dev/staging/prod) from the request "
    "and act directly — the policy layer gates sensitive actions. When a tool "
    "returns a [policy_denied] or [approval_required] marker, tell the user the "
    "action was blocked by policy and why."
)


@function_tool
def read_logs(service: str, env: str) -> str:
    """Return recent log lines for `service` in `env` (dev/staging/prod)."""
    return f"(stub) {service}@{env}: 200 OK, 200 OK, WARN upstream slow"


@function_tool
def restart_service(service: str, env: str) -> str:
    """Restart `service` in `env`."""
    return f"(stub) restarted {service}@{env}"


@function_tool
def scale_deployment(service: str, replicas: int, env: str) -> str:
    """Scale `service` to `replicas` pods in `env`."""
    return f"(stub) scaled {service}@{env} to {replicas} replicas"


agent = Agent(
    name="orchestrate_devops_agent",
    instructions=INSTRUCTION,
    tools=[read_logs, restart_service, scale_deployment],
    model=os.environ.get("HEXGATE_ORCHESTRATE_MODEL", "gpt-4o-mini"),
)
