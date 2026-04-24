"""Fortify platform integration — fetch agent definitions from a control plane.

When `FORTIFY_KEY` is set in the environment, `load_agent` fetches agent
definitions (agent.yaml, policy.yaml, system.md) from the Fortify API instead
of reading from disk. Enforcement downstream is unchanged — the policy flows
through the same AgentPolicy + GuardedTool pipeline as local agents.
"""

from coolagents.fortify.client import (
    DEFAULT_AGENT_NAME,
    FortifyClient,
    FortifyConfig,
    resolve_agent_name,
)
from coolagents.fortify.loader import load_fortify_agent

__all__ = [
    "DEFAULT_AGENT_NAME",
    "FortifyClient",
    "FortifyConfig",
    "load_fortify_agent",
    "resolve_agent_name",
]
