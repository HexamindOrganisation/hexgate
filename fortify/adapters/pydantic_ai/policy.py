from fortify.user_context import UserContext
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
