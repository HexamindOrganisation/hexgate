from google.adk.agents import BaseAgent

from fortify.runtime import UserContext
from fortify.adapters.google.tools import wrap_tools
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


def wrap_google_agent(
    agent: BaseAgent,
    context: UserContext,
    api_key: str,
) -> BaseAgent:
    """Wrap the Google ADK agent tools with the Fortify user policy."""
    agent_name = getattr(agent, "name", "default")
    tools = list(getattr(agent, "tools", []) or [])
    tool_names = [getattr(t, "name", getattr(t, "__name__", "tool")) for t in tools]
    policy = build_agent_policy(api_key, context, agent_name, tool_names)
    guarded_tools = wrap_tools(tools, policy)
    return agent.model_copy(update={"tools": guarded_tools})
