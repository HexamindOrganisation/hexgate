"""Create the first asianf agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.agents import create_agent as create_langchain_agent

from asianf.config.settings import Settings
from asianf.tools.fetch import fetch
from asianf.tools.websearch import web_search
from asianf.tracing.langfuse import (
    get_langfuse_handler,
    get_langfuse_runnable_config,
    observe,
)


def _load_system_prompt() -> str:
    """Load the agent system prompt from disk."""
    prompt_path = Path(__file__).parent.parent / "prompts" / "agent_system.md"
    return prompt_path.read_text(encoding="utf-8")


@observe(name="create_asianf_agent")
def create_agent(
    settings: Settings,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
):
    """Create an agent configured for the first asianf spike."""
    tools = [web_search, fetch]
    agent = create_langchain_agent(
        model=settings.model,
        tools=tools,
        system_prompt=_load_system_prompt(),
    )

    handler = get_langfuse_handler(
        session_id=session_id,
        user_id=user_id,
        tags=["asianf", settings.search_engine, settings.model],
    )
    return agent, handler


@observe(name="invoke_asianf_agent")
async def invoke_agent(agent: Any, handler: Any, query: str) -> dict:
    """Invoke the agent for a single query."""
    return await agent.ainvoke(
        {"messages": [{"role": "user", "content": query}]},
        config=get_langfuse_runnable_config(handler),
    )


@observe(name="stream_asianf_agent")
async def stream_agent(agent: Any, handler: Any, query: str):
    """Stream message chunks from the agent."""
    async for event in agent.astream(
        {"messages": [{"role": "user", "content": query}]},
        config=get_langfuse_runnable_config(handler),
        stream_mode="messages",
    ):
        yield event
