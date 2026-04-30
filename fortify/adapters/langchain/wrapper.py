import os
from langgraph.graph.state import CompiledStateGraph
from langchain_core.tools import BaseTool

from fortify.user_context import UserContext
from fortify.security import AgentPolicy, BaseToolPolicy
from fortify.adapters.langchain.agent import FortifyLangchainAgent
from fortify.adapters.langchain.tools import wrap_tools


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


def wrap_langchain_agent(
    *,
    agent: CompiledStateGraph,
    tools: list[BaseTool],
    user_context: UserContext,
    api_key: str | None = None,
) -> FortifyLangchainAgent:
    """Wrap the Langchain agent with the Fortify tool policy and observability.

    Args:
        agent: The Langchain agent to wrap
        tools: The Langchain tools the agent has been instantiated with
        context: The user context
        api_key: The Fortify API key
    """
    resolved_key = api_key or os.getenv("FORTIFY_KEY")
    if not resolved_key:
        raise ValueError(
            "No API key provided. Pass api_key= explicitly or set FORTIFY_KEY environment variable."
        )

    agent_name = getattr(agent, "name", "default")
    policy = build_agent_policy(
        resolved_key, user_context, agent_name, [tool.name for tool in tools]
    )
    # Mutate the caller's tool instances in place.
    wrap_tools(tools, policy)

    return FortifyLangchainAgent(
        agent=agent,
        user_context=user_context,
    )
