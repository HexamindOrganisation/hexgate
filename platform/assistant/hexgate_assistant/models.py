"""The assistant's own tables, in its own database (``hexgate_assistant``).

Written only by the assistant's code, never by a tool the model can call.
``user_id``, ``project_id`` and ``org_id`` are plain ids: they live in the
platform's database, and no foreign key crosses the two.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# A 64-bit key that still autoincrements on SQLite, where only INTEGER
# PRIMARY KEY does.
BigId = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}


class AssistantSession(Base):
    __tablename__ = "assistant_session"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[str] = mapped_column(String)
    user_id: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(default=utcnow)


class AssistantMessage(Base):
    __tablename__ = "assistant_message"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assistant_session.id"))
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String)
    content: Mapped[Any] = mapped_column(JSON)
    format_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        # Also the index a session's history is loaded by.
        UniqueConstraint("session_id", "seq"),
        CheckConstraint("kind IN ('messages', 'event')", name="ck_message_kind"),
    )


class AssistantUsage(Base):
    """One row per turn. ``session_id`` is a plain id, so a purged session
    leaves its spend counted."""

    __tablename__ = "assistant_usage"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String)
    user_id: Mapped[str] = mapped_column(String)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    model: Mapped[str] = mapped_column(String)
    input_tokens: Mapped[int] = mapped_column(BigInteger)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger)
    output_tokens: Mapped[int] = mapped_column(BigInteger)
    cost_micros: Mapped[int] = mapped_column(BigInteger)
    cost_source: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        Index("ix_assistant_usage_user_id_created_at", "user_id", "created_at"),
    )


class AssistantTurnLock(Base):
    """At most one row per user: one turn at a time across sessions,
    projects and orgs."""

    __tablename__ = "assistant_turn_lock"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    turn_id: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
