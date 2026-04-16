"""Create thin LangChain agent helpers for coolagents."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Self, TypeAlias

from langchain.agents import create_agent as create_langchain_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain.agents.structured_output import ResponseFormat
from langchain_core.caches import BaseCache
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.messages.system import SystemMessage
from langchain_core.runnables.schema import StreamEvent as LangChainStreamEvent
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from pydantic import BaseModel

from coolagents.runtime import (
    LocalWorkspace,
    ToolUseContext,
    reset_current_tool_use_context,
    set_current_tool_use_context,
)
from coolagents.streaming import new_root_run_id, normalize_langchain_events
from coolagents.stream import StreamEvent
from coolagents.tracing.langfuse import (
    CallbackHandler,
    get_langfuse_handler,
    get_langfuse_runnable_config,
    observe,
)

LangChainAgentGraph: TypeAlias = CompiledStateGraph
ToolSpec: TypeAlias = BaseTool | Callable[..., Any] | dict[str, Any]
AgentState: TypeAlias = dict[str, Any]
AgentInput: TypeAlias = str | Sequence[object] | Mapping[str, object] | BaseModel
ActionPayload: TypeAlias = dict[str, Any]
ActionContext: TypeAlias = dict[str, Any] | None
BeforeActionHook: TypeAlias = Callable[[ActionPayload, ActionContext], object | Awaitable[object]]
ContextProvider: TypeAlias = Callable[[], ActionContext]
DEFAULT_SYSTEM_PROMPT = Path(__file__).parent.parent / "prompts" / "agent_system.md"


def _build_langchain_agent(
    model: str | BaseChatModel,
    tools: Sequence[ToolSpec],
    system_prompt: str | SystemMessage | None,
    *,
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
) -> LangChainAgentGraph:
    """Build the underlying LangChain compiled graph."""
    return create_langchain_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
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


def _coerce_message_text(content: object) -> str | None:
    """Return readable text content from a message-like payload."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts) or None


def _extract_query_from_messages(messages: Sequence[object]) -> str:
    """Return the last user message text from a message list when possible."""
    for message in reversed(messages):
        if isinstance(message, BaseMessage):
            if getattr(message, "type", None) != "human":
                continue
            text = _coerce_message_text(message.content)
            if text:
                return text
            continue
        if isinstance(message, Mapping):
            role = message.get("role")
            if role not in {"user", "human"}:
                continue
            text = _coerce_message_text(message.get("content"))
            if text:
                return text
            continue
        if isinstance(message, tuple) and len(message) >= 2 and message[0] in {"user", "human"}:
            text = _coerce_message_text(message[1])
            if text:
                return text
    return ""


def normalize_input(input: AgentInput) -> AgentState:
    """Normalize wrapper-friendly input into LangChain agent state."""
    if isinstance(input, str):
        return {"messages": [{"role": "user", "content": input}]}
    if isinstance(input, BaseModel):
        return dict(input.model_dump(exclude_none=True))
    if isinstance(input, Mapping):
        return dict(input)
    return {"messages": list(input)}


def extract_input_text(input: AgentInput) -> str:
    """Extract readable user text from wrapper-friendly input."""
    if isinstance(input, str):
        return input
    if isinstance(input, BaseModel):
        return extract_input_text(input.model_dump(exclude_none=True))
    if isinstance(input, Mapping):
        query = input.get("query")
        if isinstance(query, str):
            return query
        messages = input.get("messages")
        if isinstance(messages, Sequence) and not isinstance(messages, str):
            return _extract_query_from_messages(messages)
        return ""
    return _extract_query_from_messages(input)


def _resolve_tool_use_context(
    agent: "CoolAgent",
    tool_use_context: ToolUseContext | None,
) -> ToolUseContext:
    """Return the runtime tool context for a run."""
    agent_name = getattr(agent, "name", None)
    if tool_use_context is not None:
        if tool_use_context.agent_name is None:
            tool_use_context.agent_name = agent_name
        if tool_use_context.workspace is None:
            tool_use_context.workspace = LocalWorkspace(Path.cwd())
        return tool_use_context
    return ToolUseContext(
        workspace=LocalWorkspace(Path.cwd()),
        agent_name=agent_name,
    )


class CoolAgent:
    """A small wrapper around a LangChain agent graph with room for layering."""

    def __init__(
        self,
        *,
        graph: LangChainAgentGraph,
        model: str | BaseChatModel,
        tools: Sequence[ToolSpec],
        system_prompt: str | SystemMessage | None,
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
    ) -> None:
        self.graph = graph
        self.model = model
        self.tools = list(tools)
        self.system_prompt = system_prompt
        self.middleware = tuple(middleware)
        self.response_format = response_format
        self.state_schema = state_schema
        self.context_schema = context_schema
        self.checkpointer = checkpointer
        self.store = store
        self.interrupt_before = interrupt_before
        self.interrupt_after = interrupt_after
        self.debug = debug
        self.name = name
        self.cache = cache

    async def ainvoke(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Delegate invocation to the underlying graph."""
        return await self.graph.ainvoke(payload, config=config)

    async def astream_events(
        self,
        payload: dict[str, Any],
        config: dict[str, Any],
        *,
        version: str,
    ) -> AsyncIterator[LangChainStreamEvent]:
        """Delegate event streaming to the underlying graph."""
        async for event in self.graph.astream_events(payload, config=config, version=version):
            yield event

    def with_tools(self, tools: Sequence[ToolSpec]) -> Self:
        """Rebuild the runtime with a new tool list."""
        graph = _build_langchain_agent(
            model=self.model,
            tools=tools,
            system_prompt=self.system_prompt,
            middleware=self.middleware,
            response_format=self.response_format,
            state_schema=self.state_schema,
            context_schema=self.context_schema,
            checkpointer=self.checkpointer,
            store=self.store,
            interrupt_before=self.interrupt_before,
            interrupt_after=self.interrupt_after,
            debug=self.debug,
            name=self.name,
            cache=self.cache,
        )
        return type(self)(
            graph=graph,
            model=self.model,
            tools=tools,
            system_prompt=self.system_prompt,
            middleware=self.middleware,
            response_format=self.response_format,
            state_schema=self.state_schema,
            context_schema=self.context_schema,
            checkpointer=self.checkpointer,
            store=self.store,
            interrupt_before=self.interrupt_before,
            interrupt_after=self.interrupt_after,
            debug=self.debug,
            name=self.name,
            cache=self.cache,
        )

    def enforce_policy(self, policy: object) -> Self:
        """Return a new agent runtime with Gate 1 policy enforcement applied."""
        from coolagents.agent.security import wrap_tools_with_policy
        from coolagents.security import load_policy

        return self.with_tools(wrap_tools_with_policy(self.tools, load_policy(policy)))

    def with_before_action(
        self,
        before_action: BeforeActionHook,
        *,
        context_provider: ContextProvider | None = None,
    ) -> Self:
        """Return a new agent runtime with a pre-tool Gate 2 hook applied."""
        from coolagents.agent.security import wrap_tools_with_before_action

        return self.with_tools(
            wrap_tools_with_before_action(
                self.tools,
                before_action,
                context_provider=context_provider,
                agent_name=self.name,
            )
        )


AgentGraph: TypeAlias = CoolAgent


@observe(name="create_coolagents_agent")
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
    """Create a coolagents agent as a thin wrapper over LangChain."""
    resolved_system_prompt = (
        system_prompt
        if isinstance(system_prompt, SystemMessage)
        else load_system_prompt(system_prompt)
    )
    graph = _build_langchain_agent(
        model=model,
        tools=tools,
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
    agent = CoolAgent(
        graph=graph,
        model=model,
        tools=tools,
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
        tags=tags or ["coolagents", str(model)],
    )
    return agent, handler


@observe(name="invoke_coolagents_agent")
async def invoke_agent(
    agent: AgentGraph,
    handler: CallbackHandler,
    input: AgentInput,
    *,
    tool_use_context: ToolUseContext | None = None,
) -> dict[str, Any]:
    """Invoke the agent for one normalized input payload."""
    token = set_current_tool_use_context(_resolve_tool_use_context(agent, tool_use_context))
    try:
        return await agent.ainvoke(
            normalize_input(input),
            config=get_langfuse_runnable_config(handler),
        )
    finally:
        reset_current_tool_use_context(token)


async def stream_agent_raw(
    agent: AgentGraph,
    handler: CallbackHandler,
    input: AgentInput,
    *,
    tool_use_context: ToolUseContext | None = None,
) -> AsyncIterator[LangChainStreamEvent]:
    """Stream raw LangChain events from the agent runtime."""
    config = get_langfuse_runnable_config(handler)
    config["run_id"] = new_root_run_id()
    token = set_current_tool_use_context(_resolve_tool_use_context(agent, tool_use_context))
    try:
        async for event in agent.astream_events(
            normalize_input(input),
            config=config,
            version="v2",
        ):
            yield event
    finally:
        reset_current_tool_use_context(token)


@observe(name="stream_coolagents_agent")
async def stream_agent(
    agent: AgentGraph,
    handler: CallbackHandler,
    input: AgentInput,
    *,
    tool_use_context: ToolUseContext | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream normalized runtime events from the agent."""
    raw_events = (
        stream_agent_raw(agent, handler, input)
        if tool_use_context is None
        else stream_agent_raw(agent, handler, input, tool_use_context=tool_use_context)
    )
    async for event in normalize_langchain_events(
        raw_events,
        query=extract_input_text(input),
    ):
        yield event
