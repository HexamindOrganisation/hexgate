"""One turn: the user's lock, the session, the project at turn start, the
agent's run and what it leaves stored.

The turn runs in a task of its own, never in the response that streams it:
a browser that disconnects cancels the response, and a turn cancelled with
it would lose the messages of a change it had just applied. The task runs
to its deadline, stores what it got to and releases the lock whether or not
anyone is still listening.
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import anyio
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelContextWindowExceededError,
    ModelRequestTimeout,
    OutputTokenLimitExceeded,
    RunTimedOut,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import AgentStreamEvent, ModelMessage, ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hexgate_assistant import agent as agent_module
from hexgate_assistant import events
from hexgate_assistant.agent import (
    INSTRUCTIONS,
    MCP_CALL_SECONDS,
    TURN_LIMITS,
    function_tools,
)
from hexgate_assistant.auth import AssistantToken
from hexgate_assistant.changes import (
    APPLY_SCOPE,
    applied_calls,
    redact_calls,
    redact_history,
)
from hexgate_assistant.context import (
    CONTEXT_FULL_MESSAGE,
    BudgetGuard,
    ContextFull,
    Editor,
    ProjectState,
    ProjectUnreadable,
    definition_tokens,
    editor_text,
    estimate_tokens,
    latest_change_text,
    project_card,
    read_project,
    read_reference,
    user_message,
    without_instructions,
)
from hexgate_assistant.deps import TurnDeps, TurnWrites
from hexgate_assistant.lock import TURN_DEADLINE, turn_lock
from hexgate_assistant.models import AssistantSession, utcnow
from hexgate_assistant.sessions import CHANGE_EVENT, SessionStore

logger = logging.getLogger(__name__)

# A comment line goes out after this long without an event, so neither a
# proxy nor the browser takes a long model request (up to 120 s) for a dead
# stream.
PING_SECONDS = 15
# How long a turn waits for its messages and usage to be stored, whatever
# ends it: inside the shutdown's own wait (server.SHUTDOWN_SECONDS).
STORE_SECONDS = 4
# A new session's title: the start of its first message.
TITLE_CHARS = 80
# Which instructions a session started under.
PROMPT_VERSION = hashlib.sha256(INSTRUCTIONS.encode()).hexdigest()[:12]

# What a turn's queue holds: server-sent events, then None at its end.
Events = asyncio.Queue[str | None]


@dataclass(frozen=True)
class TurnRequest:
    session_id: uuid.UUID | None
    message: str
    editor: Editor


@dataclass
class Turns:
    """What every turn shares: built once at start-up."""

    db: async_sessionmaker[AsyncSession]
    agent: Agent[TurnDeps, str]
    mcp_url: str
    # Tokens the conversation may fill (see `context_budget`).
    budget: int
    # The policy reference, the same for every turn: read by the first turn
    # that can, and again by the next one while it fails.
    reference: str | None = None
    # Estimated tokens of the tool definitions the model is offered with
    # every request, read with the reference.
    tool_tokens: int = 0
    store: SessionStore = field(init=False)
    # Turn tasks still running, held so the event loop keeps them.
    running: set[asyncio.Task[None]] = field(init=False, default_factory=set)

    def __post_init__(self) -> None:
        self.store = SessionStore(self.db)

    async def stop(self, seconds: float) -> None:
        """Cancel the turns still running, once each, and wait at most
        `seconds` for them to store and release."""
        running = list(self.running)
        for task in running:
            task.cancel()
        if running:
            await asyncio.wait(running, timeout=seconds)

    @property
    def prefix_tokens(self) -> int:
        """What every request sends before the history: the instructions,
        the reference and the tool definitions."""
        return estimate_tokens(INSTRUCTIONS + (self.reference or "")) + self.tool_tokens


async def start(
    turns: Turns, token: AssistantToken, project_id: str, request: TurnRequest
) -> Events:
    """Start a turn in its own task and return its events once it will run.

    Raises :class:`~hexgate_assistant.lock.TurnBusy`,
    :class:`~hexgate_assistant.sessions.SessionNotFound` or
    :class:`ProjectUnreadable` when it will not; nothing is streamed then.
    """
    ready: asyncio.Future[Events] = asyncio.get_running_loop().create_future()
    # A refusal nobody waits for any more (the request was cancelled) is
    # read here, not logged as never retrieved.
    ready.add_done_callback(_retrieve)
    task = asyncio.create_task(_turn(turns, token, project_id, request, ready))
    turns.running.add(task)
    task.add_done_callback(turns.running.discard)
    # Shielded: a request cancelled while it waits leaves the turn running.
    return await asyncio.shield(ready)


def _retrieve(future: asyncio.Future[Events]) -> None:
    if not future.cancelled():
        future.exception()


async def stream(queue: Events) -> AsyncIterator[str]:
    """The turn's events, with a ping after every :data:`PING_SECONDS` of
    silence. Cancelling it leaves the turn running."""
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), PING_SECONDS)
        except TimeoutError:
            yield events.PING
            continue
        if item is None:
            return
        yield item


async def _turn(
    turns: Turns,
    token: AssistantToken,
    project_id: str,
    request: TurnRequest,
    ready: asyncio.Future[Events],
) -> None:
    try:
        async with turn_lock(turns.db, token.subject) as (turn_id, started_at):
            # Before the session: a turn refused here leaves no empty chat.
            project = await _read_project(turns, token, project_id)
            session = await turns.store.open(
                user_id=token.subject,
                project_id=project_id,
                session_id=request.session_id,
                title=request.message[:TITLE_CHARS],
                prompt_version=PROMPT_VERSION,
            )
            # Under the lock: it stores an interrupted result for a call a
            # live turn of this user would still be running.
            history = await turns.store.history(session)
            change = await turns.store.latest_change(session)
            prompt = _prompt(token, project, change, request)
            queue: Events = asyncio.Queue()
            ready.set_result(queue)
            try:
                run = _Run(turns, token, project_id, session, turn_id, queue)
                await run.execute(history, prompt, project.snapshot, started_at)
            finally:
                queue.put_nowait(None)
    except asyncio.CancelledError:
        ready.cancel()
        raise
    except Exception as exc:
        if ready.done():
            logger.exception("turn of %s failed after its stream opened", token.subject)
        else:
            # Refused before the stream opened: the request answers it.
            ready.set_exception(exc)


def _prompt(
    token: AssistantToken,
    project: ProjectState,
    change: dict[str, Any] | None,
    request: TurnRequest,
) -> str:
    """The turn's user message."""
    return user_message(
        card=project_card(
            project, may_write=APPLY_SCOPE in token.scopes, today=date.today()
        ),
        latest_change=latest_change_text(change),
        editor=editor_text(request.editor),
        message=request.message,
    )


async def _read_project(
    turns: Turns, token: AssistantToken, project_id: str
) -> ProjectState:
    """The turn-start reads, in one MCP session as the user. Any failure is
    :class:`ProjectUnreadable`."""
    mcp: MCPToolset[None] = MCPToolset(
        turns.mcp_url, auth=token.token, read_timeout=MCP_CALL_SECONDS
    )
    try:
        async with mcp:
            if turns.reference is None:
                tool_tokens = await _tool_tokens(mcp, turns.agent)
                turns.reference = await read_reference(mcp)
                turns.tool_tokens = tool_tokens
            return await read_project(mcp, project_id)
    except Exception as exc:
        # Which failure (a 401 or 403 from MCP, a connection refused), never
        # the token; the answer stays 502 whichever it is.
        logger.warning(
            "turn-start reads failed (sub %s, project %s): %s",
            token.subject,
            project_id,
            _describe(exc),
        )
        if isinstance(exc, ProjectUnreadable):
            raise
        raise ProjectUnreadable(type(exc).__name__) from exc


async def _tool_tokens(mcp: MCPToolset[None], agent: Agent[TurnDeps, str]) -> int:
    """The estimated size of the tool definitions the model is offered: each
    allowlisted MCP tool's name, description and input schema, as the server
    lists them, and the agent's own tools'."""
    listed = [
        (t.name, t.description, t.input_schema)
        for t in await mcp.list_tools()
        if t.name in agent_module.ALLOWLIST
    ]
    return definition_tokens(listed + function_tools(agent))


def _describe(exc: BaseException) -> str:
    """The innermost errors of `exc`, by class and message, cut short. A
    :class:`ProjectUnreadable` already says why in its message, so the chain
    stops there."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe(e) for e in exc.exceptions)
    if exc.__cause__ is not None and not isinstance(exc, ProjectUnreadable):
        return _describe(exc.__cause__)
    return f"{type(exc).__name__}: {str(exc)[:200]}"


class _Run:
    """The agent's run of one turn, from RUN_STARTED to what it stores."""

    def __init__(
        self,
        turns: Turns,
        token: AssistantToken,
        project_id: str,
        session: AssistantSession,
        turn_id: str,
        queue: Events,
    ) -> None:
        self._turns = turns
        self._token = token
        self._project_id = project_id
        self._session = session
        self._turn_id = turn_id
        self._queue = queue
        self._translator = events.EventTranslator()
        # The run's own count, live; kept even when the run raises.
        self._usage = RunUsage()
        self._writes = (
            TurnWrites(
                project_id=project_id,
                session_id=str(session.id),
                on_change=self._changed,
            )
            if APPLY_SCOPE in token.scopes
            else None
        )
        # The turn's `change` event once sent, stored with its messages.
        self._change: events.Event | None = None

    async def execute(
        self,
        history: list[ModelMessage],
        prompt: str,
        snapshot: Mapping[str, str],
        started_at: datetime,
    ) -> None:
        thread_id = str(self._session.id)
        self._send(events.run_started(thread_id, self._turn_id))
        deadline = (started_at + TURN_DEADLINE - utcnow()).total_seconds()
        with capture_run_messages() as captured:
            try:
                await self._turns.agent.run(
                    prompt,
                    message_history=history,
                    instructions=self._turns.reference,
                    deps=TurnDeps(
                        self._token.token, snapshot=snapshot, writes=self._writes
                    ),
                    usage_limits=TURN_LIMITS,
                    timeout=max(deadline, 0),
                    event_stream_handler=self._forward,
                    capabilities=[
                        # First: the budget is measured without the contents
                        # an applied call no longer carries.
                        ProcessHistory(redact_history),
                        ProcessHistory(
                            BudgetGuard(self._turns.budget, self._turns.prefix_tokens)
                        ),
                    ],
                )
                final = events.run_finished(thread_id, self._turn_id)
            except Exception as exc:
                final = events.run_error(*self._ended_by(exc))
            finally:
                # Stored however the run ended, a shutdown's cancellation
                # included, and before the stream ends, so a message sent
                # the moment it ends finds this turn in its history.
                await _shielded(
                    self._store(_new_messages(captured[len(history) :])),
                    STORE_SECONDS,
                    f"turn {self._turn_id}: store",
                )
        self._send(final)

    def _ended_by(self, exc: Exception) -> tuple[str | None, str]:
        """The run error for `exc`, logged once: which turn, whose, why."""
        code, message = _error(exc)
        where = (self._turn_id, self._token.subject, self._project_id)
        if code is None:
            logger.error("turn %s (sub %s, project %s) failed", *where, exc_info=exc)
        else:
            logger.warning(
                "turn %s (sub %s, project %s) ended: %s (%s)",
                *where,
                code,
                type(exc).__name__,
            )
        return code, message

    async def _forward(
        self, ctx: RunContext[TurnDeps], stream: AsyncIterator[AgentStreamEvent]
    ) -> None:
        self._usage = ctx.usage
        async for event in stream:
            for out in self._translator.translate(event):
                self._send(out)

    def _changed(self, value: dict[str, Any]) -> None:
        self._change = events.custom(CHANGE_EVENT, value)
        self._send(self._change)

    async def _store(self, new: list[ModelMessage]) -> None:
        if not new:
            return
        model = self._turns.agent.model
        assert isinstance(model, Model)
        # The last call's contents too, when no request followed its result.
        new = redact_calls(new, applied_calls(self._writes))
        try:
            await self._turns.store.append_messages(
                self._session, without_instructions(new)
            )
            await self._turns.store.record_usage(
                self._session,
                org_id=self._token.org,
                model=model.model_name,
                usage=self._usage,
                # Priced by the budget's own change; until then, unpriced.
                cost_micros=0,
                cost_source="none",
            )
            # After the usage: the spend is recorded whatever this costs.
            if self._change is not None:
                await self._turns.store.append_event(self._session, self._change)
        except Exception:
            logger.exception("turn %s: storing its messages failed", self._turn_id)

    def _send(self, event: events.Event) -> None:
        self._queue.put_nowait(events.sse(event))


def _new_messages(new: list[ModelMessage]) -> list[ModelMessage]:
    """What the run added to the session. A run that got no answer from the
    model (its first request refused, over budget or failed, which Pydantic
    AI records as a response with no parts) adds nothing: the session then
    stops growing."""
    answered = any(isinstance(m, ModelResponse) and m.parts for m in new)
    return new if answered else []


# The run error code and message for each way a run can end early.
_ERRORS: tuple[tuple[type[BaseException], str, str], ...] = (
    (RunTimedOut, "timeout", "The answer took too long and was stopped."),
    (ContextFull, "context_full", CONTEXT_FULL_MESSAGE),
    # The provider's own window, when it is smaller than the budget assumed.
    (ModelContextWindowExceededError, "context_full", CONTEXT_FULL_MESSAGE),
    (UsageLimitExceeded, "limit_reached", "This answer reached its step limit."),
    (
        OutputTokenLimitExceeded,
        "limit_reached",
        "This answer reached its length limit.",
    ),
    # Before ModelAPIError, which it subclasses: one request past its own
    # deadline, every retry included.
    (ModelRequestTimeout, "timeout", "The answer took too long and was stopped."),
    (ModelAPIError, "provider", "The model provider failed."),
    (
        UnexpectedModelBehavior,
        "provider",
        "The model answered in a way it cannot use.",
    ),
)


def _error(exc: BaseException) -> tuple[str | None, str]:
    for kind, code, message in _ERRORS:
        if isinstance(exc, kind):
            return code, message
    return None, "The assistant failed."


async def _shielded(work: Awaitable[None], seconds: float, what: str) -> None:
    """Run `work` in a task of its own and wait for it at most `seconds`,
    whatever cancels the caller in the meantime (a shutdown's
    ``task.cancel()``, or Starlette's anyio scope): a cancellation is held
    until the wait ends, then raised. A task still running then is left to
    finish, and logged."""
    task = asyncio.ensure_future(work)
    _background.add(task)
    task.add_done_callback(_background.discard)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.warning("%s still running after %s s", what, seconds)
            break
        try:
            with anyio.move_on_after(remaining, shield=True):
                await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            break
    if cancelled is not None:
        raise cancelled


# Work still running after its caller stopped waiting; held so the event
# loop keeps it.
_background: set[asyncio.Future[None]] = set()
