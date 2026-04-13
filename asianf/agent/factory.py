"""Create the first asianf deep agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from asianf.config.settings import Settings
from asianf.tools.fetch import fetch
from asianf.tools.websearch import web_search
from asianf.tracing.langfuse import (
    get_langfuse_handler,
    get_langfuse_runnable_config,
    observe,
)


def _load_system_prompt() -> str:
    prompt_path = Path(__file__).parent.parent / "prompts" / "agent_system.md"
    return prompt_path.read_text(encoding="utf-8")


@observe(name="create_asianf_agent")
def create_agent(
    settings: Settings,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
):
    """Create a deep agent configured for the first asianf spike."""
    try:
        from deepagents import create_deep_agent
    except Exception as exc:  # pragma: no cover - dependency absent during scaffold phase
        raise RuntimeError(
            "deepagents is not installed. Install project dependencies first."
        ) from exc

    tools = [web_search, fetch]
    agent = create_deep_agent(
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
    """Invoke the deep agent for a single query."""
    return await agent.ainvoke(
        {"messages": [{"role": "user", "content": query}]},
        config=get_langfuse_runnable_config(handler),
    )


@observe(name="stream_asianf_agent")
async def stream_agent(agent: Any, handler: Any, query: str):
    """Stream updates from the deep agent."""
    async for event in agent.astream(
        {"messages": [{"role": "user", "content": query}]},
        config=get_langfuse_runnable_config(handler),
        stream_mode="updates",
    ):
        yield event
