import copy
import os

from pydantic_ai import Agent
from pydantic_ai.tools import Tool

from fortify.user_context import UserContext
from fortify.security import AgentPolicy, BaseToolPolicy
from fortify.adapters.pydantic_ai.agent import FortifyPydanticAgent
from fortify.adapters.pydantic_ai.tools import wrap_tools


def build_agent_policy(
    api_key: str,
    context: UserContext,
    agent_name: str,
    tool_names: list[str],
) -> AgentPolicy:
    """Build the relevant agent policy for the user"""
    # mint_token = retrieve_mint_token(api_key, context, agent_name)
    # policy = retrieve_policy(mint_token)
    return AgentPolicy(
        tools={name: BaseToolPolicy(mode="allow") for name in tool_names}
    )


def _extract_tools(agent: Agent) -> list[Tool]:
    """Extract the Tool instances registered on `agent`.

    Pydantic AI normalizes both constructor-passed tools and
    `@agent.tool` / `@agent.tool_plain` registrations into the
    same `_function_toolset.tools` dict, keyed by tool name.
    """
    toolset = getattr(agent, "_function_toolset", None)
    tools = getattr(toolset, "tools", None) if toolset is not None else None
    if tools is None:
        return []
    return list(tools.values())


def _clone_agent_with_tools(agent: Agent, wrapped_tools: list[Tool]) -> Agent:
    """Return a shallow copy of `agent` whose function toolset holds `wrapped_tools`."""
    agent_copy = copy.copy(agent)
    agent_copy.instrument = True
    toolset = getattr(agent, "_function_toolset", None)
    if toolset is not None:
        toolset_copy = copy.copy(toolset)
        toolset_copy.tools = {t.name: t for t in wrapped_tools}
        agent_copy._function_toolset = toolset_copy
    return agent_copy


def wrap_pydantic_agent(
    *,
    agent: Agent,
    user_context: UserContext,
    api_key: str | None = None,
) -> FortifyPydanticAgent:
    """Wrap the pydantic_ai agent with the Fortify tool policy and observability.

    Returns a `FortifyPydanticAgent` backed by a clone of `agent` whose
    tools are gated by the policy. The caller's original `agent` is not
    mutated, so it can be reused or wrapped again with a different
    user/policy independently.

    Args:
        agent: The pydantic_ai agent to wrap. Tools are read directly off
            the agent, so any tool registered via the constructor or via
            `@agent.tool` / `@agent.tool_plain` is gated.
        user_context: The user context
        api_key: The Fortify API key
    """
    Agent.instrument_all()
    resolved_key = api_key or os.getenv("FORTIFY_KEY")
    if not resolved_key:
        raise ValueError(
            "No API key provided. Pass api_key= explicitly or set FORTIFY_KEY environment variable."
        )

    agent_name = getattr(agent, "name", None) or "default"
    tools = _extract_tools(agent)
    policy = build_agent_policy(
        resolved_key, user_context, agent_name, [tool.name for tool in tools]
    )
    wrapped_tools = wrap_tools(tools, policy)
    cloned_agent = _clone_agent_with_tools(agent, wrapped_tools)

    return FortifyPydanticAgent(
        agent=cloned_agent,
        user_context=user_context,
    )
