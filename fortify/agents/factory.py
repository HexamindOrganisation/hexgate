"""Create thin LangChain agent helpers for fortify."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, TypeAlias

if TYPE_CHECKING:
    # Optional seam-attribute types — referenced only in __init__ signatures
    # / annotations. Imported under TYPE_CHECKING to avoid the runtime cycle
    # (security.* and cloud.* both eventually import from this module).
    from fortify.cloud.client import FortifyClient
    from fortify.security.enforcer import PolicyEnforcer
    from fortify.security.source import PolicySource

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

from fortify import audit
from fortify.runtime import (
    LocalWorkspace,
    ToolUseContext,
    Workspace,
    reset_current_tool_use_context,
    set_current_tool_use_context,
)
from fortify.security.decision import Decision
from fortify.streaming import StreamEvent, new_root_run_id, normalize_langchain_events
from fortify.tracing.langfuse import (
    CallbackHandler,
    get_langfuse_handler,
    get_langfuse_runnable_config,
    observe,
)

LangChainAgentGraph: TypeAlias = CompiledStateGraph
ToolSpec: TypeAlias = BaseTool | Callable[..., Any] | dict[str, Any]
AgentState: TypeAlias = dict[str, Any]
AgentInput: TypeAlias = str | Sequence[object] | Mapping[str, object] | BaseModel
ApprovalHandler: TypeAlias = bool | Callable[[Decision], bool | Awaitable[bool]]
DEFAULT_SYSTEM_PROMPT = Path(__file__).parent / "prompts" / "agent_system.md"


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
        if (
            isinstance(message, tuple)
            and len(message) >= 2
            and message[0] in {"user", "human"}
        ):
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


def _resolve_user_facts(agent: "FortifyAgent") -> dict[str, list[str | int]] | None:
    """Lazily attenuate when a :class:`User` scope is active.

    Returns the extracted facts dict for the active user, or ``None`` if
    no User scope is in play, the agent isn't cloud-bound, or attenuation
    fails (logged as a warning — the agent runs without facts and any
    predicate requiring them will fail-closed).
    """
    from fortify.runtime.context import get_current_user

    user = get_current_user()
    if user is None:
        return None
    client = agent.fortify_client
    if client is None:
        # Local agent or test stub — User scope is set but there's nothing to
        # attenuate against. Surface a single warning so devs see why their
        # `requires_user` predicate isn't firing on a local-loaded agent.
        import logging

        logging.getLogger(__name__).warning(
            "User scope active but agent has no fortify_client; "
            "biscuit_facts will be empty (use load_fortify_agent for attenuation)"
        )
        return None
    from fortify.cloud.attenuate import attenuate_for_user
    from fortify.cloud.biscuit import (
        TokenError,
        TokenSignatureError,
        extract_facts,
        parse_envelope,
    )

    try:
        pub = client.public_key_bytes()
        child_envelope = attenuate_for_user(
            client.config.api_key,
            pub,
            user=user.user_id,
            role=user.role,
            ttl_seconds=user.ttl_seconds,
        )
        _, _, biscuit_b64 = parse_envelope(child_envelope)
        return extract_facts(biscuit_b64, pub)
    except (TokenError, TokenSignatureError) as exc:
        import logging

        logging.getLogger(__name__).warning(
            "user-scope attenuation failed: %s; agent runs without facts", exc
        )
        return None


def _resolve_tool_use_context(
    agent: "FortifyAgent",
    tool_use_context: ToolUseContext | None,
) -> ToolUseContext:
    """Return the runtime tool context for a run.

    Resolution order for the workspace:
    1. ``tool_use_context.workspace`` — caller-supplied at invocation time.
    2. ``agent.workspace`` — wired in at ``create_agent(...)``-time.
    3. ``LocalWorkspace(Path.cwd())`` — last-resort default.

    When ``tool_use_context`` is None and an :class:`~fortify.runtime.User`
    scope is active, this also runs lazy biscuit attenuation against the
    agent's bound ``fortify_client`` and folds the resulting facts into the
    fresh context. An explicit ``tool_use_context`` argument always wins —
    that's how callers pass their own facts in (e.g. tests, or production
    code that wants to bypass the User scope for a specific call).
    """
    agent_name = getattr(agent, "name", None)
    agent_workspace = getattr(agent, "workspace", None)
    fallback_workspace = agent_workspace or LocalWorkspace(Path.cwd())
    if tool_use_context is not None:
        if tool_use_context.agent_name is None:
            tool_use_context.agent_name = agent_name
        if tool_use_context.workspace is None:
            tool_use_context.workspace = fallback_workspace
        return tool_use_context
    return ToolUseContext(
        workspace=fallback_workspace,
        agent_name=agent_name,
        biscuit_facts=_resolve_user_facts(agent),
    )


class FortifyAgent:
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
        workspace: Workspace | None = None,
        enforcer: "PolicyEnforcer | None" = None,
        policy_source: "PolicySource | None" = None,
        fortify_client: "FortifyClient | None" = None,
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
        self.workspace = workspace
        # Enforcement seam. Three optional fields populated by the loaders
        # (load_fortify_agent, _local_policy_override) and the
        # ``enforce_policy`` builder. Promoted from setattr/getattr-with-
        # default to first-class fields so the type checker covers the
        # refresh path and ``with_tools`` rebuilds can't silently drop them
        # via a misspelled attribute name.
        self._enforcer: "PolicyEnforcer | None" = enforcer
        self._policy_source: "PolicySource | None" = policy_source
        self.fortify_client: "FortifyClient | None" = fortify_client

    async def ainvoke(
        self, payload: dict[str, Any], config: dict[str, Any]
    ) -> dict[str, Any]:
        """Delegate invocation to the underlying graph.

        Refreshes the attached policy source before delegating — see
        :func:`_refresh_policy_safely`. The refresh seam lives here (not
        only in :func:`invoke_agent`) so a direct caller of
        ``agent.ainvoke(...)`` gets hot-reload too, instead of silently
        running with stale policy.
        """
        await _refresh_policy_safely(self)
        return await self.graph.ainvoke(payload, config=config)

    async def astream_events(
        self,
        payload: dict[str, Any],
        config: dict[str, Any],
        *,
        version: str,
    ) -> AsyncIterator[LangChainStreamEvent]:
        """Delegate event streaming to the underlying graph.

        Refreshes the attached policy source before delegating, same as
        :meth:`ainvoke`. Wrapping both methods means hot-reload fires
        regardless of which entry point a caller picks.
        """
        await _refresh_policy_safely(self)
        async for event in self.graph.astream_events(
            payload, config=config, version=version
        ):
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
        # Thread the enforcement seam through the rebuild so policy
        # refresh + lazy user attenuation keep working after with_tools.
        # The enforcer / policy_source pair is what makes refresh_policy()
        # able to swap engines without re-wrapping every tool; the client
        # is what load_fortify_agent attached for cloud-side attenuation.
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
            workspace=self.workspace,
            enforcer=self._enforcer,
            policy_source=self._policy_source,
            fortify_client=self.fortify_client,
        )

    def enforce_policy(
        self,
        policy: object,
        *,
        approval_handler: ApprovalHandler | None = None,
    ) -> Self:
        """Return a new agent with Gate 1 policy enforcement applied.

        ``policy`` may be a YAML path, a ``policies/`` directory,
        :class:`AgentPolicy`, :class:`PolicySet`, a
        :class:`~fortify.security.PolicyBundle` (the WASM enforcement
        path), or ``None`` (no-op). Role resolves at call time from the
        active :class:`User`. ``approval_handler`` (callable or ``bool``)
        resolves NEEDS_APPROVAL inline; ``None`` renders structured errors.
        """
        from langchain_core.tools import BaseTool

        from fortify.adapters.langchain.tools import GuardedTool
        from fortify.security.bundle import PolicyBundle
        from fortify.security.enforcer import PolicyEnforcer
        from fortify.security.policy_set import PolicySet, load_policy_set

        if policy is None:
            return self.with_tools(list(self.tools))

        if isinstance(policy, PolicyBundle):
            resolved = policy
        elif isinstance(policy, PolicySet):
            resolved = policy
        else:
            resolved = load_policy_set(policy)
        audit_sender = audit.configure()
        enforcer = PolicyEnforcer(
            resolved, agent_name=self.name or "default", audit_sender=audit_sender
        )

        wrapped: list[ToolSpec] = []
        for tool_spec in self.tools:
            if isinstance(tool_spec, BaseTool):
                wrapped.append(
                    GuardedTool.wrap(
                        tool_spec,
                        enforcer=enforcer,
                        approval_handler=approval_handler,
                    )
                )
            else:
                wrapped.append(tool_spec)
        rebuilt = self.with_tools(wrapped)
        # Stash the enforcer on the rebuilt agent so refresh_policy() can
        # swap its policy in place when the source serves a new bundle,
        # without rebuilding the tool wrappers. ``self`` stays untouched.
        rebuilt._enforcer = enforcer
        # Detach any inherited source so a later refresh can't swap the
        # explicit policy back out; callers wanting hot reload re-attach.
        rebuilt._policy_source = None
        return rebuilt

    def refresh_policy(self) -> None:
        """Pull the current policy from the attached source and swap it in.

        Runs at the top of every agent run (:meth:`ainvoke` /
        :meth:`astream_events`). Delegates the swap — ETag/304, identity
        check, fail-soft — to :class:`~fortify.security.binding.PolicyBinding`.
        No-op without an enforcer + source.
        """
        if self._policy_source is None or self._enforcer is None:
            return
        from fortify.security.binding import PolicyBinding

        PolicyBinding(self._enforcer, self._policy_source).refresh()


AgentGraph: TypeAlias = FortifyAgent


def enforce_policy(
    agent: AgentGraph,
    policy: object,
    *,
    approval_handler: ApprovalHandler | None = None,
) -> AgentGraph:
    """Functional alias for :meth:`FortifyAgent.enforce_policy`."""
    return agent.enforce_policy(policy, approval_handler=approval_handler)


@observe(name="create_fortify_agent")
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
    workspace: Workspace | None = None,
    bind_policy: bool | None = None,
    approval_handler: ApprovalHandler | None = None,
) -> tuple[AgentGraph, CallbackHandler]:
    """Create a fortify agent as a thin wrapper over LangChain.

    ``bind_policy``: ``None`` (auto) binds when ``FORTIFY_KEY`` or
    ``FORTIFY_LOCAL_POLICY`` is set and ``name`` is given; ``True``
    always binds (raises without a name); ``False`` never binds.
    Binding gates the tools and attaches a refresh source, like
    ``load_fortify_agent``. ``approval_handler`` applies on that path.
    """
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
    agent = FortifyAgent(
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
        workspace=workspace,
    )
    if _should_bind_policy(bind_policy, name):
        agent = _bind_policy(agent, name, approval_handler)  # type: ignore[arg-type]

    handler = get_langfuse_handler(
        session_id=session_id,
        user_id=user_id,
        tags=tags or ["fortify", str(model)],
    )
    return agent, handler


def _should_bind_policy(bind_policy: bool | None, name: str | None) -> bool:
    """Decide whether :func:`create_agent` binds policy at creation."""
    if bind_policy is False:
        return False
    if bind_policy is True:
        if not name:
            raise ValueError(
                "create_agent(bind_policy=True) requires name=... — the "
                "agent name is the policy lookup key on the platform."
            )
        return True
    return bool(name) and bool(
        os.environ.get("FORTIFY_KEY") or os.environ.get("FORTIFY_LOCAL_POLICY")
    )


def _bind_policy(
    agent: "FortifyAgent",
    name: str,
    approval_handler: ApprovalHandler | None,
) -> "FortifyAgent":
    """Resolve the policy for ``name`` and enforce it on ``agent``.

    Mirrors ``load_fortify_agent``: resolve → enforce → attach the
    refresh source. A 404 registers the agent from its in-code
    definition and resolves again; other failures stay loud.
    """
    from fortify.cloud.client import FortifyError
    from fortify.security.binding import PolicyBinding

    client = None
    if os.environ.get("FORTIFY_KEY"):
        from fortify.cloud.client import FortifyClient, FortifyConfig

        client = FortifyClient(FortifyConfig.from_env())

    try:
        binding = PolicyBinding.resolve(name, client=client)
    except FortifyError as exc:
        if exc.status != 404:
            raise
        from fortify.cli.register import register_agent

        _logger.info("agent %r not registered — registering it from code", name)
        register_agent(agent)
        binding = PolicyBinding.resolve(name, client=client)

    enforced = agent.enforce_policy(
        binding.enforcer.policy, approval_handler=approval_handler
    )
    enforced._policy_source = binding.source
    enforced.fortify_client = client
    return enforced


_logger = logging.getLogger("fortify.agents.factory")


async def _refresh_policy_safely(agent: "FortifyAgent") -> None:
    """Pull the latest policy from the agent's attached source, off the loop.

    Called by :meth:`FortifyAgent.ainvoke` / :meth:`astream_events` at the
    top of every async invocation, so policy changes land at the next run
    regardless of which entry point a caller picks (the high-level
    :func:`invoke_agent` / :func:`stream_agent_raw` wrappers go through
    those methods too, so they get refresh for free).

    No-op when no source is attached (programmatic construction).
    Failures log a warning at WARNING level and keep the previous policy
    — a transient network blip never crashes a chat turn. The log line
    is the only signal today; programmatic observability (counter /
    hook / last_refreshed_at) isn't exposed.
    """
    try:
        await asyncio.to_thread(agent.refresh_policy)
    except Exception as exc:  # noqa: BLE001 — refresh failures must not crash the run
        _logger.warning(
            "policy refresh failed: %s — keeping previously loaded policy", exc
        )


@observe(name="invoke_fortify_agent")
async def invoke_agent(
    agent: AgentGraph,
    handler: CallbackHandler,
    input: AgentInput,
    *,
    tool_use_context: ToolUseContext | None = None,
) -> dict[str, Any]:
    """Invoke the agent for one normalized input payload.

    Policy refresh is handled by :meth:`FortifyAgent.ainvoke` itself,
    so direct callers of that method see the same hot-reload behaviour
    as callers of this wrapper. No double-refresh.
    """
    token = set_current_tool_use_context(
        _resolve_tool_use_context(agent, tool_use_context)
    )
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
    """Stream raw LangChain events from the agent runtime.

    Policy refresh is handled by :meth:`FortifyAgent.astream_events`
    itself — see :func:`invoke_agent` for the matching rationale.
    """
    config = get_langfuse_runnable_config(handler)
    config["run_id"] = new_root_run_id()
    token = set_current_tool_use_context(
        _resolve_tool_use_context(agent, tool_use_context)
    )
    try:
        async for event in agent.astream_events(
            normalize_input(input),
            config=config,
            version="v2",
        ):
            yield event
    finally:
        reset_current_tool_use_context(token)


@observe(name="stream_fortify_agent")
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
