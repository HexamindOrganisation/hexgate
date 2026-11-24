"""The session store: a user's conversations, their messages and their spend.

Every way in to a session goes through :meth:`SessionStore.open`, which
finds it only for the user and project of the caller's token: a session id
alone, guessed or leaked, never reads another user's chat. The other
methods take the :class:`AssistantSession` that ``open`` returned.
"""

import logging
import uuid
from collections.abc import Sequence
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    repair_messages,
)
from pydantic_ai.usage import RunUsage
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hexgate_assistant.models import (
    AssistantMessage,
    AssistantSession,
    AssistantUsage,
    utcnow,
)

logger = logging.getLogger(__name__)

# The shape a `messages` row's content is written in: Pydantic AI's message
# JSON as of the pinned version. A bump that fails the frozen-row test in
# tests/test_sessions.py raises it, with a conversion of the older rows.
FORMAT_VERSION = 1


class SessionNotFound(Exception):
    """No session with this id for this user and project."""


class SessionStore:
    def __init__(self, db: async_sessionmaker[AsyncSession]) -> None:
        self._db = db

    async def open(
        self,
        *,
        user_id: str,
        project_id: str,
        session_id: uuid.UUID | None,
        title: str,
        prompt_version: str,
    ) -> AssistantSession:
        """The caller's session `session_id`, or a new one when it is None.

        `user_id` is the token's `sub`. Raises :class:`SessionNotFound` for
        an id that is not this user's in this project, the same as for an
        id that does not exist.
        """
        async with self._db() as db:
            if session_id is None:
                row = AssistantSession(
                    project_id=project_id,
                    user_id=user_id,
                    title=title,
                    prompt_version=prompt_version,
                )
                db.add(row)
                await db.commit()
                return row
            row = await db.scalar(
                select(AssistantSession).where(
                    AssistantSession.id == session_id,
                    AssistantSession.user_id == user_id,
                    AssistantSession.project_id == project_id,
                )
            )
        if row is None:
            raise SessionNotFound(session_id)
        return row

    async def history(self, session: AssistantSession) -> list[ModelMessage]:
        """The session's messages, ready to send to a model.

        A tool call left without a result (a turn that died mid-tool) gets
        an `interrupted` result, stored, since providers reject an
        unanswered call. Call it only under the user's turn lock: a call
        still running in a live turn would be stored as interrupted.
        """
        async with self._db() as db:
            contents = await db.scalars(
                select(AssistantMessage.content)
                .where(
                    AssistantMessage.session_id == session.id,
                    AssistantMessage.kind == "messages",
                )
                .order_by(AssistantMessage.seq)
            )
            stored = ModelMessagesTypeAdapter.validate_python(
                [m for content in contents for m in content]
            )
        # The same clean-up minus closing the last response's calls, which
        # also merges a stored repair into the turn that followed it: the
        # baseline the repair is then measured against.
        baseline = repair_messages(stored, repair_last_response=False)
        repaired = repair_messages(baseline)
        added = repaired[len(baseline) :]
        if repaired[: len(baseline)] != baseline:
            # Calls closed inside a request that already holds some of their
            # results (a turn that died between parallel tool calls) cannot
            # be stored as an append; the repair still runs on every load.
            logger.warning("session %s: history repaired, not stored", session.id)
        elif added:
            await self.append_messages(session, added)
        return repaired

    async def append_messages(
        self, session: AssistantSession, messages: Sequence[ModelMessage]
    ) -> None:
        content = ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")
        await self._append(session, "messages", content)

    async def append_event(
        self, session: AssistantSession, event: dict[str, Any]
    ) -> None:
        await self._append(session, "event", event)

    async def record_usage(
        self,
        session: AssistantSession,
        *,
        org_id: str,
        model: str,
        usage: RunUsage,
        cost_micros: int,
        cost_source: str,
    ) -> None:
        """One turn's spend. The row outlives the session it names."""
        async with self._db() as db:
            db.add(
                AssistantUsage(
                    org_id=org_id,
                    user_id=session.user_id,
                    session_id=session.id,
                    model=model,
                    input_tokens=usage.input_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    output_tokens=usage.output_tokens,
                    cost_micros=cost_micros,
                    cost_source=cost_source,
                )
            )
            await db.commit()

    async def _append(self, session: AssistantSession, kind: str, content: Any) -> None:
        # The user's turn lock keeps a session to one writer, so max + 1
        # does not race; the unique (session_id, seq) refuses it if it did.
        async with self._db() as db:
            last = await db.scalar(
                select(func.max(AssistantMessage.seq)).where(
                    AssistantMessage.session_id == session.id
                )
            )
            db.add(
                AssistantMessage(
                    session_id=session.id,
                    seq=(last or 0) + 1,
                    kind=kind,
                    content=content,
                    format_version=FORMAT_VERSION,
                )
            )
            await db.execute(
                update(AssistantSession)
                .where(AssistantSession.id == session.id)
                .values(last_used_at=utcnow())
            )
            await db.commit()
