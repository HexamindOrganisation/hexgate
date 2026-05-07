import os
from langgraph.graph.state import CompiledStateGraph
from langchain_core.tools import BaseTool

from fortify.adapters.langchain.agent import FortifyLangchainAgent
from fortify.adapters.langchain.tools import wrap_tools


def wrap_langchain_agent(
    *,
    agent: CompiledStateGraph,
    tools: list[BaseTool],
    api_key: str | None = None,
) -> FortifyLangchainAgent:
    """Wrap the Langchain agent with the Fortify tool policy and observability.

    The returned proxy expects a `user_context` keyword argument on each
    invocation method (`invoke`, `ainvoke`, `stream`, `astream`,
    `astream_events`). The active policy is resolved per call from that
    context, so a single wrapped agent can serve many users concurrently.

    Args:
        agent: The Langchain agent to wrap.
        tools: The Langchain tools the agent has been instantiated with.
            Mutated in place to install policy gates that read from a
            `ContextVar`.
        api_key: The Fortify API key. Falls back to the `FORTIFY_KEY`
            environment variable.
    """
    resolved_key = api_key or os.getenv("FORTIFY_KEY")
    if not resolved_key:
        raise ValueError(
            "No API key provided. Pass api_key= explicitly or set FORTIFY_KEY environment variable."
        )

    # Mutate the caller's tool instances in place; idempotent.
    wrap_tools(tools)

    return FortifyLangchainAgent(
        agent=agent,
        api_key=resolved_key,
        tool_names=[tool.name for tool in tools],
    )
