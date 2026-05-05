import dataclasses
import os
from agents import Agent

from fortify.user_context import UserContext
from fortify.adapters.openai.tools import wrap_tools
from fortify.security import AgentPolicy, BaseToolPolicy


def build_agent_policy(
    api_key: str,
    context: UserContext,
    agent_name: str,
    tool_names: list[str],
) -> AgentPolicy:
    """Build the relevant agent policy for the user"""
    # TODO: Implement the logic to build the agent policy based on the user context
    # mint_token = retrieve_mint_token(api_key, context, agent_name)
    # policy = retrieve_policy(mint_token)
    return AgentPolicy(
        tools={name: BaseToolPolicy(mode="allow") for name in tool_names}
    )


def wrap_openai_agent(
    agent: Agent,
    context: UserContext,
    api_key: str,
) -> Agent:
    """Wrap the OpenAI agent tools with the Fortify user policy."""
    agent_name = getattr(agent, "name", "default")
    tool_names = [tool.name for tool in agent.tools]
    policy = build_agent_policy(api_key, context, agent_name, tool_names)
    guarded_tools = wrap_tools(agent.tools, policy)
    # return a copy of the agent with the guarded tools
    return dataclasses.replace(agent, tools=guarded_tools)
