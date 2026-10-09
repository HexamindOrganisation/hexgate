"""The two OAuth tables, round-tripped column for column through ``create_all``."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import DEFAULT_USER_ID
from hexgate_api.models import OAuthAuthorizationCode, OAuthRefreshToken, utcnow


def _code(**overrides) -> OAuthAuthorizationCode:
    now = utcnow()
    fields = {
        "code_hash": "c" * 64,
        "user_id": DEFAULT_USER_ID,
        "client_id": "https://claude.ai/oauth/claude-code-client-metadata",
        "redirect_uri": "http://localhost:33418/callback",
        "scopes_csv": "policy:read,audit:read",
        "projects_csv": "p1,p2",
        "resource": "http://localhost:8000/mcp",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "created_at": now,
        "expires_at": now + timedelta(seconds=60),
    }
    return OAuthAuthorizationCode(**(fields | overrides))


def _refresh(**overrides) -> OAuthRefreshToken:
    now = utcnow()
    fields = {
        "token_hash": "r" * 64,
        "user_id": DEFAULT_USER_ID,
        "client_id": "https://claude.ai/oauth/claude-code-client-metadata",
        "client_name": "Claude Code",
        "scopes_csv": "policy:read",
        "projects_csv": "p1",
        "resource": "http://localhost:8000/mcp",
        "family_id": "fam-1",
        "created_at": now,
        "last_used_at": now,
        "expires_at": now + timedelta(days=30),
    }
    return OAuthRefreshToken(**(fields | overrides))


async def _round_trip(session: AsyncSession, row: SQLModel) -> SQLModel:
    session.add(row)
    await session.commit()
    session.expunge_all()
    return await session.get(type(row), row.id)


def _columns(row: SQLModel) -> dict:
    # SQLite drops tzinfo on read, so compare datetimes as naive UTC.
    return {
        k: v.replace(tzinfo=None) if hasattr(v, "tzinfo") else v
        for k, v in row.model_dump().items()
    }


async def test_create_all_happy_path(engine) -> None:
    def indexed_columns(conn) -> set[tuple[str, ...]]:
        indexes = inspect(conn).get_indexes("oauth_refresh_token")
        return {tuple(index["column_names"]) for index in indexes}

    async with engine.connect() as conn:
        indexed = await conn.run_sync(indexed_columns)

    assert {("family_id",), ("user_id",), ("replaced_by_id",)} <= indexed


@pytest.mark.parametrize("make_row", [_code, _refresh])
async def test_round_trip_happy_path(session, make_row) -> None:
    row = make_row()
    expected = _columns(row)

    loaded = await _round_trip(session, row)

    assert _columns(loaded) == expected


async def test_when_refresh_token_is_rotated_then_old_row_links_the_new_one(
    session,
) -> None:
    old = await _round_trip(session, _refresh())
    new = await _round_trip(
        session, _refresh(token_hash="s" * 64, family_id=old.family_id)
    )

    old.revoked_at = utcnow()
    old.revoked_by_user_id = DEFAULT_USER_ID
    old.replaced_by_id = new.id
    loaded = await _round_trip(session, old)

    assert loaded.replaced_by_id == new.id
    assert loaded.revoked_by_user_id == DEFAULT_USER_ID


@pytest.mark.parametrize("make_row", [_code, _refresh])
async def test_when_hash_repeats_then_insert_is_refused(session, make_row) -> None:
    await _round_trip(session, make_row())
    session.add(make_row())

    with pytest.raises(IntegrityError):
        await session.commit()
