import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage
from sqlalchemy import select

from hexgate_assistant.models import AssistantMessage, AssistantSession, AssistantUsage
from hexgate_assistant.sessions import FORMAT_VERSION, SessionNotFound, SessionStore

USER = "user-a"
PROJECT = "project-1"

ANSWERED = [
    ModelRequest(parts=[UserPromptPart("list the files")]),
    ModelResponse(parts=[ToolCallPart("policy_list_files", {}, tool_call_id="c1")]),
    ModelRequest(
        parts=[ToolReturnPart("policy_list_files", "policy.yaml", tool_call_id="c1")]
    ),
    ModelResponse(parts=[TextPart("One file: policy.yaml.")]),
]
# A turn that died while its tool ran: the call was stored, its result never.
DANGLING = [
    ModelRequest(parts=[UserPromptPart("read it")]),
    ModelResponse(
        parts=[
            ToolCallPart("policy_read_file", {"name": "policy.yaml"}, tool_call_id="c2")
        ]
    ),
]


@pytest.fixture
def store(db) -> SessionStore:
    return SessionStore(db)


async def _open(store, *, user_id=USER, project_id=PROJECT, session_id=None):
    return await store.open(
        user_id=user_id,
        project_id=project_id,
        session_id=session_id,
        title="t",
        prompt_version="1",
    )


async def _rows(db, session_id) -> list[AssistantMessage]:
    async with db() as s:
        rows = await s.scalars(
            select(AssistantMessage)
            .where(AssistantMessage.session_id == session_id)
            .order_by(AssistantMessage.seq)
        )
        return list(rows)


async def test_open_happy_path(store):
    created = await _open(store)

    found = await _open(store, session_id=created.id)

    assert (found.id, found.user_id, found.project_id) == (created.id, USER, PROJECT)


@pytest.mark.parametrize(
    "other",
    [
        {"user_id": "user-b"},
        {"project_id": "project-2"},
        {"session_id": uuid.uuid4()},
    ],
    ids=["another user", "another project", "unknown id"],
)
async def test_when_session_is_not_the_callers_then_not_found(store, other):
    created = await _open(store)

    with pytest.raises(SessionNotFound):
        await _open(store, **{"session_id": created.id, **other})


async def test_history_happy_path(store, db):
    session = await _open(store)
    await store.append_messages(session, ANSWERED[:2])
    await store.append_event(session, {"type": "change", "change_id": "chg_1"})
    await store.append_messages(session, ANSWERED[2:])

    assert await store.history(session) == ANSWERED
    rows = await _rows(db, session.id)
    assert [(r.seq, r.kind) for r in rows] == [
        (1, "messages"),
        (2, "event"),
        (3, "messages"),
    ]
    assert {r.format_version for r in rows} == {FORMAT_VERSION}


async def test_when_two_users_have_sessions_then_each_sees_only_its_own(store, db):
    a = await _open(store)
    b = await _open(store, user_id="user-b")
    await store.append_messages(a, ANSWERED)
    b_used = await _last_used_at(db, b.id)

    await store.append_messages(b, DANGLING[:1])

    assert await store.history(a) == ANSWERED
    assert await store.history(b) == DANGLING[:1]
    assert [r.seq for r in await _rows(db, a.id)] == [1]
    assert [r.seq for r in await _rows(db, b.id)] == [1]
    a_used = await _last_used_at(db, a.id)
    await store.append_messages(b, ANSWERED[1:2])
    assert await _last_used_at(db, a.id) == a_used
    assert await _last_used_at(db, b.id) > b_used
    with pytest.raises(SessionNotFound):
        await _open(store, user_id="user-b", session_id=a.id)


async def test_when_a_repair_cannot_be_appended_then_it_is_not_stored(
    store, db, caplog
):
    # Two parallel calls, one result stored: the missing one goes into that
    # request, which an append cannot change.
    session = await _open(store)
    await store.append_messages(
        session,
        [
            ModelRequest(parts=[UserPromptPart("read both")]),
            ModelResponse(
                parts=[
                    ToolCallPart("policy_read_file", {}, tool_call_id="a"),
                    ToolCallPart("policy_read_file", {}, tool_call_id="b"),
                ]
            ),
            ModelRequest(parts=[ToolReturnPart("policy_read_file", "x", "a")]),
        ],
    )

    history = await store.history(session)

    assert [p.outcome for p in history[-1].parts] == ["success", "interrupted"]
    assert len(await _rows(db, session.id)) == 1
    assert "history repaired, not stored" in caplog.text


async def test_when_a_tool_call_has_no_result_then_history_repairs_and_stores_it(
    store, db
):
    session = await _open(store)
    await store.append_messages(session, ANSWERED + DANGLING)

    history = await store.history(session)

    (part,) = history[-1].parts
    assert isinstance(part, ToolReturnPart)
    assert (part.tool_call_id, part.outcome) == ("c2", "interrupted")
    assert history[:-1] == ANSWERED + DANGLING
    # Stored: the next load finds nothing to repair and adds no row.
    assert len(await _rows(db, session.id)) == 2
    assert await store.history(session) == history
    assert len(await _rows(db, session.id)) == 2


async def _last_used_at(db, session_id):
    async with db() as s:
        return await s.scalar(
            select(AssistantSession.last_used_at).where(
                AssistantSession.id == session_id
            )
        )


async def test_when_a_repaired_session_goes_on_then_later_repairs_are_stored(store, db):
    session = await _open(store)
    await store.append_messages(session, DANGLING)
    await store.history(session)
    # The next turn: its prompt request follows the stored repair.
    await store.append_messages(session, ANSWERED)
    await store.history(session)
    rows = len(await _rows(db, session.id))

    await store.append_messages(session, DANGLING)
    history = await store.history(session)

    assert history[-1].parts[0].outcome == "interrupted"
    assert len(await _rows(db, session.id)) == rows + 2


async def test_when_a_row_was_written_by_format_version_1_then_it_loads(store, db):
    # Written by pydantic-ai 2.71.0. A bump that reads it differently must
    # come with a new FORMAT_VERSION and a conversion of the stored rows.
    assert FORMAT_VERSION == 1
    at = datetime(2026, 11, 23, tzinfo=UTC)
    frozen = Path(__file__).with_name("stored_messages_v1.json").read_text()
    session = await _open(store)
    async with db() as s:
        s.add(
            AssistantMessage(
                session_id=session.id,
                seq=1,
                kind="messages",
                content=json.loads(frozen),
                format_version=1,
            )
        )
        await s.commit()

    assert await store.history(session) == [
        ModelResponse(
            parts=[ToolCallPart("policy_read_file", {"name": "policy.yaml"}, "c1")],
            timestamp=at,
        ),
        ModelRequest(
            parts=[ToolReturnPart("policy_read_file", "version: 1", "c1", timestamp=at)]
        ),
    ]


async def test_when_messages_are_appended_then_last_used_at_moves(store, db):
    session = await _open(store)
    before = await _last_used_at(db, session.id)

    await store.append_messages(session, ANSWERED)

    assert await _last_used_at(db, session.id) > before


async def test_record_usage_happy_path(store, db):
    session = await _open(store)

    await store.record_usage(
        session,
        org_id="org-1",
        model="openrouter:some/model",
        usage=RunUsage(
            input_tokens=100,
            cache_read_tokens=40,
            cache_write_tokens=10,
            output_tokens=7,
        ),
        cost_micros=1234,
        cost_source="provider",
    )

    async with db() as s:
        row = await s.scalar(select(AssistantUsage))
    assert (row.user_id, row.session_id, row.org_id) == (USER, session.id, "org-1")
    assert (
        row.input_tokens,
        row.cache_read_tokens,
        row.cache_write_tokens,
        row.output_tokens,
        row.cost_micros,
    ) == (100, 40, 10, 7, 1234)
