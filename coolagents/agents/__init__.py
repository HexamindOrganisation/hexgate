"""Helpers for loading packaged agent definitions."""

from coolagents.agents.loader import (
    clear_registered_agents,
    list_available_agents,
    load_builtin_agent,
    load_builtin_agent_policy,
    load_builtin_agent_spec,
    load_agent,
    load_local_agent,
    load_local_agent_policy,
    load_local_agent_spec,
    list_builtin_agents,
    list_local_agents,
    list_registered_agents,
    load_registered_agent,
    register_agent,
    resolve_agent_source,
    resolve_builtin_tools,
    unregister_agent,
)
from coolagents.agents.models import AgentSpec

__all__ = [
    "AgentSpec",
    "clear_registered_agents",
    "list_available_agents",
    "list_builtin_agents",
    "list_local_agents",
    "list_registered_agents",
    "load_agent",
    "load_builtin_agent",
    "load_builtin_agent_policy",
    "load_builtin_agent_spec",
    "load_local_agent",
    "load_local_agent_policy",
    "load_local_agent_spec",
    "load_registered_agent",
    "register_agent",
    "resolve_agent_source",
    "resolve_builtin_tools",
    "unregister_agent",
]
