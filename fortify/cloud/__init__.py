"""Fortify platform integration — fetch agent definitions from a control plane.

When `FORTIFY_KEY` is set in the environment, `load_agent` fetches agent
definitions (agent.yaml, policy.yaml, system.md) from the Fortify API instead
of reading from disk. Enforcement downstream is unchanged — the policy flows
through the same AgentPolicy + GuardedTool pipeline as local agents.
"""

from fortify.cloud.biscuit import (
    TokenError,
    TokenSignatureError,
    parse_envelope,
    verify_biscuit,
)
from fortify.cloud.client import (
    DEFAULT_AGENT_NAME,
    FortifyClient,
    FortifyConfig,
    FortifyError,
    resolve_agent_name,
)
from fortify.cloud.loader import load_fortify_agent

__all__ = [
    "DEFAULT_AGENT_NAME",
    "FortifyClient",
    "FortifyConfig",
    "FortifyError",
    "TokenError",
    "TokenSignatureError",
    "load_fortify_agent",
    "parse_envelope",
    "resolve_agent_name",
    "verify_biscuit",
]
