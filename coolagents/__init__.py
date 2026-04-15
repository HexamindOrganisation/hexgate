"""Public package surface for coolagents."""

from coolagents.agent.factory import create_agent, invoke_agent, stream_agent, stream_agent_raw
from coolagents.agent.security import enforce_policy, with_before_action
from coolagents.agents.loader import (
    clear_registered_agents,
    list_available_agents,
    list_builtin_agents,
    list_local_agents,
    list_registered_agents,
    load_agent,
    load_builtin_agent,
    load_local_agent,
    load_registered_agent,
    register_agent,
    unregister_agent,
)
from coolagents.security import AgentPolicy
from coolagents.tools import agent_tool
from coolagents.tools.fetch import fetch
from coolagents.tools.websearch import web_search

__all__ = [
    "AgentPolicy",
    "agent_tool",
    "clear_registered_agents",
    "create_agent",
    "enforce_policy",
    "fetch",
    "invoke_agent",
    "list_available_agents",
    "list_builtin_agents",
    "list_local_agents",
    "list_registered_agents",
    "load_agent",
    "load_builtin_agent",
    "load_local_agent",
    "load_registered_agent",
    "register_agent",
    "stream_agent",
    "stream_agent_raw",
    "unregister_agent",
    "with_before_action",
    "web_search",
]
