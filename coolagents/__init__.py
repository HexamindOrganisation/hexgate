"""Public package surface for coolagents."""

from coolagents.agent.factory import create_agent, invoke_agent, stream_agent, stream_agent_raw
from coolagents.agent.security import enforce_policy, with_approval_handler, with_before_action
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
from coolagents.runtime import LocalWorkspace, ToolUseContext, Workspace
from coolagents.security import AgentPolicy
from coolagents.tools import agent_tool, edit_file, glob, grep, read_file, write_file
from coolagents.tools.fetch import fetch
from coolagents.tools.websearch import web_search

__all__ = [
    "AgentPolicy",
    "LocalWorkspace",
    "ToolUseContext",
    "Workspace",
    "agent_tool",
    "edit_file",
    "clear_registered_agents",
    "create_agent",
    "enforce_policy",
    "fetch",
    "glob",
    "grep",
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
    "read_file",
    "stream_agent",
    "stream_agent_raw",
    "unregister_agent",
    "with_approval_handler",
    "with_before_action",
    "web_search",
    "write_file",
]
