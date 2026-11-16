"""The rows of a grant: the one-time authorization code, then the refresh
tokens of the login it starts, rotated at each use.

A code or refresh token is 32 random bytes, base64url; only its SHA-256 is
stored, so a database read gives no one a usable token.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from mcp.shared.auth import OAuthToken
from sqlalchemy import case, delete, func, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.oauth_tokens import ACCESS_TOKEN_TTL_SECONDS, mint_access_token
from hexgate_api.features.oauth.authorization import CheckedAuthorization
from hexgate_api.models import (
    OAuthAuthorizationCode,
    OAuthRefreshToken,
    new_uuid_str,
    utcnow,
)
from hexgate_api.settings import get_settings

CODE_TTL = timedelta(seconds=60)
REFRESH_TOKEN_TTL = timedelta(days=30)
# However often it is refreshed, a login ends this long after the consent.
MAX_LOGIN_AGE = timedelta(days=90)
# A rotated-away token presented again this soon is a retry, not a theft.
REFRESH_REUSE_WINDOW = timedelta(seconds=30)
# A code no login names, and a dead login, are kept this long, then purged.
CODE_RETENTION = timedelta(hours=1)
REFRESH_TOKEN_RETENTION = timedelta(days=7)
PURGE_BATCH = 1000
# A refresh row's own state; a rotation copies every other column.
_ROW_STATE = {
    "id",
    "token_hash",
    "last_used_at",
    "expires_at",
    "revoked_at",
    "revoked_by_user_id",
    "replaced_by_id",
}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _csv(values: list[str]) -> str:
    return ",".join(values)


def scopes_of(row: OAuthAuthorizationCode | OAuthRefreshToken) -> list[str]:
    return row.scopes_csv.split(",")


def projects_of(row: OAuthAuthorizationCode | OAuthRefreshToken) -> list[str]:
    return row.projects_csv.split(",")


async def issue_code(
    session: AsyncSession,
    *,
    user_id: str,
    request: CheckedAuthorization,
    projects: list[str],
) -> str:
    """Write the code row for a consented request and return the code."""
    code = secrets.token_urlsafe(32)
    session.add(
        OAuthAuthorizationCode(
            code_hash=_hash(code),
            user_id=user_id,
            client_id=request.client.client_id,
            # Normalised, as the MCP SDK's /token normalises the one it compares.
            redirect_uri=str(request.redirect_uri),
            scopes_csv=_csv(request.scopes),
            projects_csv=_csv(projects),
            resource=request.resource,
            code_challenge=request.code_challenge,
            expires_at=utcnow() + CODE_TTL,
        )
    )
    await session.commit()
    return code


async def redeem_code(
    session: AsyncSession, code: str
) -> OAuthAuthorizationCode | None:
    """Mark the code used and return its row, or None if it is unknown, used or expired.

    One conditional UPDATE, so of two concurrent redemptions only one gets the
    row. The code is spent before the caller checks the client, redirect or
    verifier, so a mismatch burns it too.
    """
    now = utcnow()
    result = await session.exec(
        update(OAuthAuthorizationCode)
        .where(
            OAuthAuthorizationCode.code_hash == _hash(code),
            OAuthAuthorizationCode.used_at.is_(None),
            OAuthAuthorizationCode.expires_at > now,
        )
        .values(used_at=now)
        .returning(OAuthAuthorizationCode)
    )
    row = result.scalar_one_or_none()
    await session.commit()
    return row


async def issue_tokens(
    session: AsyncSession,
    *,
    user_id: str,
    client_id: str,
    client_name: str,
    scopes: list[str],
    projects: list[str],
    resource: str,
    authorization_code_id: str,
) -> OAuthToken | None:
    """Write the first refresh row of a new login and return it with an access
    token; None if the code was replayed since it was spent.

    The check locks the code row, in the transaction that inserts the refresh
    row: a replay marking the code waits for this commit, and then sees the
    new row to revoke.
    """
    result = await session.exec(
        select(OAuthAuthorizationCode.id)
        .where(
            OAuthAuthorizationCode.id == authorization_code_id,
            OAuthAuthorizationCode.replayed_at.is_(None),
        )
        .with_for_update()
    )
    if result.scalar_one_or_none() is None:
        await session.rollback()
        return None
    refresh_token = secrets.token_urlsafe(32)
    row_id = new_uuid_str()
    row = OAuthRefreshToken(
        id=row_id,
        family_id=row_id,  # the first token of the login names its family
        token_hash=_hash(refresh_token),
        user_id=user_id,
        client_id=client_id,
        client_name=client_name,
        scopes_csv=_csv(scopes),
        projects_csv=_csv(projects),
        resource=resource,
        authorization_code_id=authorization_code_id,
        expires_at=utcnow() + REFRESH_TOKEN_TTL,
    )
    tokens = _tokens(row, refresh_token)  # before the insert: minting can refuse
    session.add(row)
    await session.commit()
    return tokens


def login_end(row: OAuthRefreshToken) -> datetime:
    """When the login ``row`` belongs to ends, however often it is refreshed."""
    return as_utc(row.created_at) + MAX_LOGIN_AGE


def rotated_moments_ago(row: OAuthRefreshToken) -> bool:
    """Whether ``row`` was replaced by a refresh within the reuse window (not
    revoked with its whole login)."""
    return row.replaced_by_id is not None and (
        utcnow() - as_utc(row.revoked_at) < REFRESH_REUSE_WINDOW
    )


async def find_refresh_token(
    session: AsyncSession, token: str
) -> OAuthRefreshToken | None:
    """The row of ``token``, revoked or not, or None if it is unknown."""
    result = await session.exec(
        select(OAuthRefreshToken).where(OAuthRefreshToken.token_hash == _hash(token))
    )
    return result.scalar_one_or_none()


async def rotate_refresh_token(
    session: AsyncSession, old: OAuthRefreshToken
) -> OAuthToken | None:
    """Revoke ``old`` and return the tokens of its successor, or None if ``old``
    was revoked since it loaded (by a concurrent refresh, or with its login).

    The successor copies every column but the token's own state, so the grant
    (scopes, projects, the code it came from, when the user consented) carries
    over. It is written first so the
    old row's ``replaced_by_id`` names a row that exists; the conditional
    UPDATE then decides, so of two concurrent refreshes only one commits.
    """
    now = utcnow()
    refresh_token = secrets.token_urlsafe(32)
    new = OAuthRefreshToken(
        **old.model_dump(exclude=_ROW_STATE),
        token_hash=_hash(refresh_token),
        last_used_at=now,
        expires_at=min(now + REFRESH_TOKEN_TTL, login_end(old)),
    )
    tokens = _tokens(new, refresh_token)
    session.add(new)
    await session.flush()
    result = await session.exec(
        update(OAuthRefreshToken)
        .where(
            OAuthRefreshToken.id == old.id,
            OAuthRefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now, replaced_by_id=new.id)
        .returning(OAuthRefreshToken.id)
    )
    if result.scalar_one_or_none() is None:
        await session.rollback()
        return None
    await session.commit()
    return tokens


async def revoke_family(
    session: AsyncSession, family_id: str, *, user_id: str | None = None
) -> int:
    """Revoke every live token of a login; the number revoked.

    Given ``user_id``, only that user's login, recorded as revoked by them;
    without it, the platform revokes it (a reused token or code).
    """
    where = [OAuthRefreshToken.family_id == family_id]
    if user_id is not None:
        where.append(OAuthRefreshToken.user_id == user_id)
    return await _revoke_live(session, where, by_user_id=user_id)


async def revoke_replayed_code(
    session: AsyncSession, code: str
) -> tuple[OAuthAuthorizationCode | None, int]:
    """Mark a spent code replayed and revoke what it issued: its row (None if
    unknown or never spent) and the number of tokens revoked.

    Called when a code fails to redeem, so a spent one is being presented a
    second time, which means it leaked.
    """
    result = await session.exec(
        update(OAuthAuthorizationCode)
        .where(
            OAuthAuthorizationCode.code_hash == _hash(code),
            OAuthAuthorizationCode.used_at.is_not(None),
        )
        .values(replayed_at=utcnow())
        .returning(OAuthAuthorizationCode)
    )
    row = result.scalar_one_or_none()
    if row is None:
        await session.commit()
        return None, 0
    where = [OAuthRefreshToken.authorization_code_id == row.id]
    return row, await _revoke_live(session, where)


async def _revoke_live(
    session: AsyncSession, where: list, *, by_user_id: str | None = None
) -> int:
    """Revoke the live refresh rows matching ``where``; the number revoked.

    Repeated until a fresh statement sees none: on Postgres an UPDATE that
    waited on a row a concurrent refresh was rotating skips that row once
    revoked, and never saw the successor the refresh then committed.
    """
    live = [*where, OAuthRefreshToken.revoked_at.is_(None)]
    revoked = 0
    while True:
        result = await session.exec(
            update(OAuthRefreshToken)
            .where(*live)
            .values(revoked_at=utcnow(), revoked_by_user_id=by_user_id)
            .returning(OAuthRefreshToken.id)
        )
        revoked += len(result.all())
        remaining = await session.exec(select(OAuthRefreshToken.id).where(*live))
        if remaining.first() is None:
            break
    await session.commit()
    return revoked


async def live_grants(session: AsyncSession, user_id: str) -> list[OAuthRefreshToken]:
    """The user's live logins, one row each (a family's current token), newest first."""
    result = await session.exec(
        select(OAuthRefreshToken)
        .where(
            OAuthRefreshToken.user_id == user_id,
            OAuthRefreshToken.revoked_at.is_(None),
            OAuthRefreshToken.expires_at > utcnow(),
        )
        .order_by(OAuthRefreshToken.created_at.desc())
    )
    return list(result.scalars().all())


async def purge_dead_rows(session: AsyncSession) -> None:
    """Delete up to ``PURGE_BATCH`` refresh rows of dead logins, and as many
    code rows nothing needs any more.

    A login's rows go only once it is dead (no row live) and has been for
    ``REFRESH_TOKEN_RETENTION``: while it lives, a rotated-away row is what
    reveals a stolen token when presented again. A spent code stays while a
    refresh row names it, so replaying it still revokes what it issued.
    """
    now = utcnow()
    batch = (
        (
            await session.exec(
                select(OAuthRefreshToken.id)
                .where(OAuthRefreshToken.family_id.in_(_dead_logins(now)))
                .limit(PURGE_BATCH)
            )
        )
        .scalars()
        .all()
    )
    if batch:
        # A batch may cut a login's chain: unlink the rows that name the ones going.
        await session.exec(
            update(OAuthRefreshToken)
            .where(OAuthRefreshToken.replaced_by_id.in_(batch))
            .values(replaced_by_id=None)
        )
        await session.exec(
            delete(OAuthRefreshToken).where(OAuthRefreshToken.id.in_(batch))
        )
    cutoff = now - CODE_RETENTION
    issued = select(OAuthRefreshToken.authorization_code_id).where(
        OAuthRefreshToken.authorization_code_id.is_not(None)
    )
    stale_codes = (
        select(OAuthAuthorizationCode.id)
        .where(
            OAuthAuthorizationCode.expires_at < cutoff,
            OAuthAuthorizationCode.id.not_in(issued),
        )
        .limit(PURGE_BATCH)
    )
    await session.exec(
        delete(OAuthAuthorizationCode).where(OAuthAuthorizationCode.id.in_(stale_codes))
    )
    await session.commit()


def _dead_logins(now: datetime):
    """The family ids of logins that ended over ``REFRESH_TOKEN_RETENTION`` ago.

    A row ends when it is revoked or expires, whichever is first; a login ends
    with its last row, so a login with a live row never qualifies.
    """
    ended = case(
        (
            OAuthRefreshToken.revoked_at < OAuthRefreshToken.expires_at,
            OAuthRefreshToken.revoked_at,
        ),
        else_=OAuthRefreshToken.expires_at,
    )
    return (
        select(OAuthRefreshToken.family_id)
        .group_by(OAuthRefreshToken.family_id)
        .having(func.max(ended) < now - REFRESH_TOKEN_RETENTION)
    )


def _tokens(row: OAuthRefreshToken, refresh_token: str) -> OAuthToken:
    """``refresh_token``, the token of ``row``, with an access token carrying
    the row's scopes and projects."""
    settings = get_settings()
    scopes = scopes_of(row)
    access_token = mint_access_token(
        sub=row.user_id,
        client_id=row.client_id,
        scopes=scopes,
        projects=projects_of(row),
        issuer=settings.public_url,
        # The MCP server forwards this token to the API, so it names both.
        audience=[settings.mcp_resource_url, settings.public_url],
    )
    return OAuthToken(
        access_token=access_token,
        expires_in=ACCESS_TOKEN_TTL_SECONDS,
        scope=" ".join(scopes),
        refresh_token=refresh_token,
    )


def as_utc(value: datetime) -> datetime:
    """``value`` as an aware UTC datetime; SQLite hands timestamps back naive."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
