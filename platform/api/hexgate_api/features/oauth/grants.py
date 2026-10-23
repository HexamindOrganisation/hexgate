"""The rows of a grant: the one-time authorization code, then the first tokens.

A code or refresh token is 32 random bytes, base64url; only its SHA-256 is
stored, so a database read gives no one a usable token.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from mcp.shared.auth import OAuthToken
from sqlalchemy import update
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


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _csv(values: list[str]) -> str:
    return ",".join(values)


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
) -> OAuthToken:
    """Write the first refresh row of a new login and return it with an access token."""
    settings = get_settings()
    access_token = mint_access_token(
        sub=user_id,
        client_id=client_id,
        scopes=scopes,
        projects=projects,
        issuer=settings.public_url,
        # The MCP server forwards this token to the API, so it names both.
        audience=[settings.mcp_resource_url, settings.public_url],
    )
    refresh_token = secrets.token_urlsafe(32)
    row_id = new_uuid_str()
    session.add(
        OAuthRefreshToken(
            id=row_id,
            family_id=row_id,  # the first token of the login names its family
            token_hash=_hash(refresh_token),
            user_id=user_id,
            client_id=client_id,
            client_name=client_name,
            scopes_csv=_csv(scopes),
            projects_csv=_csv(projects),
            resource=resource,
            expires_at=utcnow() + REFRESH_TOKEN_TTL,
        )
    )
    await session.commit()
    return OAuthToken(
        access_token=access_token,
        expires_in=ACCESS_TOKEN_TTL_SECONDS,
        scope=" ".join(scopes),
        refresh_token=refresh_token,
    )


def as_utc(value: datetime) -> datetime:
    """``value`` as an aware UTC datetime; SQLite hands timestamps back naive."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
