"""Small CLI demo for the first asianf agent."""

from __future__ import annotations

import asyncio

from asianf.agent.factory import create_agent, stream_agent
from asianf.setup import bootstrap
from asianf.stream import BlockDeltaEvent, EventType, ToolEndEvent, ToolStartEvent
from asianf.tools.fetch import fetch
from asianf.tools.websearch import web_search
from asianf.tracing.langfuse import maybe_get_trace_url


def _render_event(event: object) -> str | None:
    """Render a normalized stream event for the CLI."""
    if isinstance(event, BlockDeltaEvent):
        return event.text
    if isinstance(event, ToolStartEvent):
        return f"\n[tool:start] {event.tool_name}\n"
    if isinstance(event, ToolEndEvent):
        return f"[tool:end] {event.tool_name}\n"
    if getattr(event, "event_type", None) == EventType.RUN_END:
        return "\n"
    return None


async def main() -> None:
    """Run the demo agent and print normalized stream events."""
    settings = bootstrap()
    agent, handler = create_agent(
        model=settings.model,
        tools=[web_search, fetch],
        session_id="demo-session",
        tags=["asianf", settings.search_engine, settings.model],
    )

    query = "Search for the latest major AI breakthroughs this month and summarize the top 5 with sources."
    print(f"Query: {query}\n")

    async for event in stream_agent(agent, handler, query):
        rendered = _render_event(event)
        if rendered:
            print(rendered, end="", flush=True)

    trace_url = maybe_get_trace_url(handler)
    if trace_url:
        print(f"\nLangfuse trace: {trace_url}")


if __name__ == "__main__":
    asyncio.run(main())
