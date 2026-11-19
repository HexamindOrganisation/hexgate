import uuid
from dataclasses import replace

import pytest
from sqlalchemy import delete, func, inspect, make_url, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.schema import CreateIndex, CreateTable
from starlette.testclient import TestClient

from hexgate_assistant.db import create_tables, session_factory
from hexgate_assistant.models import (
    AssistantMessage,
    AssistantSession,
    AssistantTurnLock,
    AssistantUsage,
    Base,
)
from hexgate_assistant.server import create_app

TABLES = {
    "assistant_session",
    "assistant_message",
    "assistant_usage",
    "assistant_turn_lock",
}


@pytest.fixture
async def db(engine):
    await create_tables(engine)
    return session_factory(engine)


@pytest.fixture
async def session_id(db):
    async with db() as s:
        row = AssistantSession(
            project_id="p", user_id="u", title="t", prompt_version="1"
        )
        s.add(row)
        await s.commit()
        return row.id


async def _table_names(engine) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda c: set(inspect(c).get_table_names()))


async def test_create_tables_happy_path(engine):
    await create_tables(engine)
    # A second start-up finds them and changes nothing.
    await create_tables(engine)

    assert await _table_names(engine) == TABLES


async def test_when_app_starts_then_tables_exist(settings, engine):
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/assistant/health").status_code == 200

    assert await _table_names(engine) == TABLES


def test_when_database_is_unreachable_then_start_up_fails(settings, tmp_path):
    missing = make_url(f"sqlite+aiosqlite:///{tmp_path / 'no' / 'such' / 'dir.db'}")
    app = create_app(replace(settings, database_url=missing))

    with pytest.raises(OperationalError), TestClient(app):
        pass


def test_postgres_ddl_happy_path():
    # Tests run on SQLite; deploy runs these tables on Postgres.
    ddl = {
        t.name: str(CreateTable(t).compile(dialect=postgresql.dialect()))
        for t in Base.metadata.sorted_tables
    }

    assert "id UUID NOT NULL" in ddl[AssistantSession.__tablename__]
    assert "id BIGSERIAL NOT NULL" in ddl[AssistantMessage.__tablename__]
    assert "id BIGSERIAL NOT NULL" in ddl[AssistantUsage.__tablename__]
    assert "created_at TIMESTAMP WITH TIME ZONE" in ddl[AssistantUsage.__tablename__]
    assert (
        "FOREIGN KEY(session_id) REFERENCES assistant_session (id) ON DELETE CASCADE"
        in ddl[AssistantMessage.__tablename__]
    )
    (index,) = AssistantUsage.__table__.indexes
    assert str(CreateIndex(index).compile(dialect=postgresql.dialect())) == (
        "CREATE INDEX ix_assistant_usage_user_id_created_at "
        "ON assistant_usage (user_id, created_at)"
    )


def _message(session_id: uuid.UUID, kind: str = "messages") -> AssistantMessage:
    return AssistantMessage(
        session_id=session_id, seq=1, kind=kind, content=[], format_version=1
    )


async def test_when_seq_repeats_in_a_session_then_refused(db, session_id):
    async with db() as s:
        s.add(_message(session_id))
        await s.commit()
        s.add(_message(session_id))

        with pytest.raises(IntegrityError):
            await s.commit()


async def test_when_kind_is_unknown_then_refused(db, session_id):
    async with db() as s:
        s.add(_message(session_id, kind="other"))

        with pytest.raises(IntegrityError):
            await s.commit()


async def test_when_user_already_holds_the_lock_then_second_row_refused(db):
    async with db() as s:
        s.add(AssistantTurnLock(user_id="u", turn_id="t1"))
        await s.commit()
        s.add(AssistantTurnLock(user_id="u", turn_id="t2"))

        with pytest.raises(IntegrityError):
            await s.commit()


async def test_when_session_is_deleted_then_its_messages_are_deleted(db, session_id):
    async with db() as s:
        s.add(_message(session_id))
        await s.commit()

        await s.execute(
            delete(AssistantSession).where(AssistantSession.id == session_id)
        )
        await s.commit()

        count = select(func.count()).select_from(AssistantMessage)
        assert await s.scalar(count) == 0
