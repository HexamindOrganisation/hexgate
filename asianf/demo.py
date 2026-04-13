"""Small CLI demo for the first asianf agent."""

from __future__ import annotations

import asyncio
from typing import Any

from asianf.agent.factory import create_agent, stream_agent
from asianf.setup import bootstrap
from asianf.tracing.langfuse import maybe_get_trace_url


def _extract_text(content: Any) -> str:
    """Best-effort extraction of visible text from streamed message content."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue

        block_type = block.get("type")
        if block_type in {"text", "text_delta", "output_text"}:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block_type == "content_block_delta":
            delta = block.get("delta")
            if isinstance(delta, dict):
                text = delta.get("text")
                if isinstance(text, str):
                    parts.append(text)

    return "".join(parts)


async def main() -> None:
    settings = bootstrap()
    agent, handler = create_agent(settings, session_id="demo-session")

    query = "What is NVIDIA OpenShell and how is it different from a normal Docker container?"
    print(f"Query: {query}\n")

    streamed_any_text = False
    async for event in stream_agent(agent, handler, query):
        message, _metadata = event
        text = _extract_text(getattr(message, "content", ""))
        if text:
            print(text, end="", flush=True)
            streamed_any_text = True

    if streamed_any_text:
        print()

    trace_url = maybe_get_trace_url(handler)
    if trace_url:
        print(f"\nLangfuse trace: {trace_url}")


if __name__ == "__main__":
    asyncio.run(main())
