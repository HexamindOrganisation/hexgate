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
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelContextWindowExceededError,
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

from hexgate_assistant import events
from hexgate_assistant.agent import (
    INSTRUCTIONS,
    MCP_CALL_SECONDS,
    TURN_LIMITS,
    TurnDeps,
)
from hexgate_assistant.auth import AssistantToken
from hexgate_assistant.context import (
    CONTEXT_FULL_MESSAGE,
    BudgetGuard,
    ContextFull,
    Editor,
    ProjectState,
    ProjectUnreadable,
    editor_text,
    estimate_tokens,
    latest_change_text,
    project_card,
    read_project,
    read_reference,
    user_message,
    without_instructions,
)
from hexgate_assistant.lock import TURN_DEADLINE, turn_lock
from hexgate_assistant.models import AssistantSession, utcnow
from hexgate_assistant.sessions import SessionStore

logger = logging.getLogger(__name__)

# A comment line goes out after this long without an event, so neither a
# proxy nor the browser takes a long model request (up to 120 s) for a dead
# stream.
PING_SECONDS = 15
# A new session's title: the start of its first message.
TITLE_CHARS = 80
APPLY_SCOPE = "policy:apply"
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
    store: SessionStore = field(init=False)
    # Turn tasks still running, held so the event loop keeps them.
    running: set[asyncio.Task[None]] = field(init=False, default_factory=set)

    def __post_init__(self) -> None:
        self.store = SessionStore(self.db)

    @property
    def prefix_tokens(self) -> int:
        """The instructions and the reference, as sent before the history."""
        return estimate_tokens(INSTRUCTIONS + (self.reference or ""))


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
                run = _Run(turns, token, session, turn_id, queue)
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
                turns.reference = await read_reference(mcp)
            return await read_project(mcp, project_id)
    except Exception as exc:
        raise ProjectUnreadable(type(exc).__name__) from exc


class _Run:
    """The agent's run of one turn, from RUN_STARTED to what it stores."""

    def __init__(
        self,
        turns: Turns,
        token: AssistantToken,
        session: AssistantSession,
        turn_id: str,
        queue: Events,
    ) -> None:
        self._turns = turns
        self._token = token
        self._session = session
        self._turn_id = turn_id
        self._queue = queue
        self._translator = events.EventTranslator()
        # The run's own count, live; kept even when the run raises.
        self._usage = RunUsage()

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
                    deps=TurnDeps(self._token.token, snapshot=snapshot),
                    usage_limits=TURN_LIMITS,
                    timeout=max(deadline, 0),
                    event_stream_handler=self._forward,
                    capabilities=[
                        ProcessHistory(
                            BudgetGuard(self._turns.budget, self._turns.prefix_tokens)
                        )
                    ],
                )
                final = events.run_finished(thread_id, self._turn_id)
            except Exception as exc:
                final = events.run_error(*_error(exc))
            finally:
                # Stored however the run ended, a shutdown's cancellation
                # included, and before the stream ends, so a message sent
                # the moment it ends finds this turn in its history.
                await self._store(_new_messages(captured[len(history) :]))
        self._send(final)

    async def _forward(
        self, ctx: RunContext[TurnDeps], stream: AsyncIterator[AgentStreamEvent]
    ) -> None:
        self._usage = ctx.usage
        async for event in stream:
            for out in self._translator.translate(event):
                self._send(out)

    async def _store(self, new: list[ModelMessage]) -> None:
        if not new:
            return
        model = self._turns.agent.model
        try:
            await self._turns.store.append_messages(
                self._session, without_instructions(new)
            )
            await self._turns.store.record_usage(
                self._session,
                org_id=self._token.org,
                model=model.model_name if isinstance(model, Model) else str(model),
                usage=self._usage,
                # Priced by the budget's own change; until then, unpriced.
                cost_micros=0,
                cost_source="none",
            )
        except Exception:
            logger.exception("turn %s: storing its messages failed", self._turn_id)

    def _send(self, event: events.Event) -> None:
        self._queue.put_nowait(events.sse(event))


def _new_messages(new: list[ModelMessage]) -> list[ModelMessage]:
    """What the run added to the session. A run that got no answer from the
    model (its first request refused, over budget or failed) adds nothing:
    the session then stops growing."""
    return new if any(isinstance(m, ModelResponse) for m in new) else []


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
    logger.error("turn failed", exc_info=exc)
    return None, "The assistant failed."
