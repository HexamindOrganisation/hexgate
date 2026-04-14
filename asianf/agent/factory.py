"""Create the first asianf agent."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any, TypeAlias

from langchain.agents.middleware.types import AgentMiddleware
from langchain.agents import create_agent as create_langchain_agent
from langchain.agents.structured_output import ResponseFormat
from langchain_core.caches import BaseCache
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages.system import SystemMessage
from langchain_core.runnables.schema import StreamEvent as LangChainStreamEvent
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore

from asianf.streaming import new_root_run_id, normalize_langchain_events
from asianf.stream import StreamEvent
from asianf.tracing.langfuse import (
    CallbackHandler,
    get_langfuse_handler,
    get_langfuse_runnable_config,
    observe,
)

AgentGraph: TypeAlias = CompiledStateGraph
ToolSpec: TypeAlias = BaseTool | Callable[..., Any] | dict[str, Any]
DEFAULT_SYSTEM_PROMPT = Path(__file__).parent.parent / "prompts" / "agent_system.md"

def _resolve_prompt_path(prompt_path: str | Path) -> Path:
    """Resolve a prompt path relative to the package root when needed."""
    path = Path(prompt_path)
    if path.is_absolute():
        return path
    if path.exists():
        return path
    return Path(__file__).parent.parent / path


def load_system_prompt(system_prompt: str | Path | None) -> str | None:
    """Load prompt contents when the input refers to a prompt file."""
    if system_prompt is None:
        return None
    if isinstance(system_prompt, Path):
        return _resolve_prompt_path(system_prompt).read_text(encoding="utf-8")
    if system_prompt.endswith((".txt", ".jinja", ".md")):
        return _resolve_prompt_path(system_prompt).read_text(encoding="utf-8")
    return system_prompt


@observe(name="create_asianf_agent")
def create_agent(
    model: str | BaseChatModel,
    tools: Sequence[ToolSpec],
    system_prompt: str | Path | SystemMessage | None = DEFAULT_SYSTEM_PROMPT,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    middleware: Sequence[AgentMiddleware[Any, Any]] = (),
    response_format: ResponseFormat[Any] | type[Any] | dict[str, Any] | None = None,
    state_schema: type[Any] | None = None,
    context_schema: type[Any] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
    interrupt_before: list[str] | None = None,
    interrupt_after: list[str] | None = None,
    debug: bool = False,
    name: str | None = None,
    cache: BaseCache[Any] | None = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Create an asianf agent as a thin wrapper over LangChain."""
    resolved_system_prompt = (
        system_prompt
        if isinstance(system_prompt, SystemMessage)
        else load_system_prompt(system_prompt)
    )
    agent = create_langchain_agent(
        model=model,
        tools=list(tools),
        system_prompt=resolved_system_prompt,
        middleware=middleware,
        response_format=response_format,
        state_schema=state_schema,
        context_schema=context_schema,
        checkpointer=checkpointer,
        store=store,
        interrupt_before=interrupt_before,
        interrupt_after=interrupt_after,
        debug=debug,
        name=name,
        cache=cache,
    )

    handler = get_langfuse_handler(
        session_id=session_id,
        user_id=user_id,
        tags=tags or ["asianf", str(model)],
    )
    return agent, handler


@observe(name="invoke_asianf_agent")
async def invoke_agent(agent: AgentGraph, handler: CallbackHandler, query: str) -> dict:
    """Invoke the agent for a single query."""
    return await agent.ainvoke(
        {"messages": [{"role": "user", "content": query}]},
        config=get_langfuse_runnable_config(handler),
    )


async def stream_agent_raw(
    agent: AgentGraph,
    handler: CallbackHandler,
    query: str,
) -> AsyncIterator[LangChainStreamEvent]:
    """Stream raw LangChain events from the agent runtime."""
    config = get_langfuse_runnable_config(handler)
    config["run_id"] = new_root_run_id()
    async for event in agent.astream_events(
        {"messages": [{"role": "user", "content": query}]},
        config=config,
        version="v2",
    ):
        yield event


@observe(name="stream_asianf_agent")
async def stream_agent(
    agent: AgentGraph,
    handler: CallbackHandler,
    query: str,
) -> AsyncIterator[StreamEvent]:
    """Stream normalized runtime events from the agent."""
    async for event in normalize_langchain_events(
        stream_agent_raw(agent, handler, query),
        query=query,
    ):
        yield event
