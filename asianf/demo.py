"""Small CLI demo for the first asianf agent."""

from __future__ import annotations

import asyncio

from asianf.agent.factory import create_agent, stream_agent
from asianf.setup import bootstrap
from asianf.tracing.langfuse import maybe_get_trace_url


async def main() -> None:
    settings = bootstrap()
    agent, handler = create_agent(settings, session_id="demo-session")

    query = "What is NVIDIA OpenShell and how is it different from a normal Docker container?"
    print(f"Query: {query}\n")

    async for event in stream_agent(agent, handler, query):
        print(event)

    trace_url = maybe_get_trace_url(handler)
    if trace_url:
        print(f"\nLangfuse trace: {trace_url}")


if __name__ == "__main__":
    asyncio.run(main())
