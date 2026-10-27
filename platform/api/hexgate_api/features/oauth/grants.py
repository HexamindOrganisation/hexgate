"""The rows of a grant: the one-time authorization code.

A code is 32 random bytes, base64url; only its SHA-256 is stored, so a
database read gives no one a usable code.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.features.oauth.authorization import CheckedAuthorization
from hexgate_api.models import OAuthAuthorizationCode, utcnow

CODE_TTL = timedelta(seconds=60)


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
