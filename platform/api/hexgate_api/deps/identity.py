"""Dashboard-user dependencies — human auth via session cookie or OAuth token.

The active user comes from the FastAPI Users session cookie, then from an
OAuth access token (``Authorization: Bearer``, MCP clients), then from an
``X-Dev-User`` header fallback that is gated behind
``HEXGATE_ALLOW_DEV_USER_HEADER`` (test-only; off in production).
"""

import logging
import os

from fastapi import Depends, Header, HTTPException, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import OAUTH_NO_PROJECT, OAUTH_SCOPE_KEY
from hexgate_api.core.biscuits import ENVELOPE_PREFIX
from hexgate_api.core.db import get_session
from hexgate_api.core.oauth_tokens import (
    AccessTokenClaims,
    InvalidAccessTokenError,
    verify_access_token,
)
from hexgate_api.features.auth.service import current_active_user_optional
from hexgate_api.models import User
from hexgate_api.settings import get_settings

_log = logging.getLogger(__name__)

# An SDK key envelope (``fty_<env>_<project>_<biscuit>``) is not an OAuth
# token; the SDK routes verify it. The underscore is part of the prefix.
_SDK_KEY_PREFIX = f"{ENVELOPE_PREFIX}_"


def _dev_user_header_allowed() -> bool:
    """Whether the ``X-Dev-User`` test-only auth seam is enabled.

    Defaults to off — production servers MUST NOT accept this header,
    since it bypasses the cookie/session check and trusts whatever
    UUID the caller asserts. Tests opt in via ``conftest.py`` setting
    ``HEXGATE_ALLOW_DEV_USER_HEADER=1`` in the environment.
    """
    return os.environ.get("HEXGATE_ALLOW_DEV_USER_HEADER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def require_user(
    request: Request,
    cookie_user: User | None = Depends(current_active_user_optional),
    authorization: str | None = Header(default=None),
    x_dev_user: str | None = Header(default=None, alias="X-Dev-User"),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the active dashboard user via session cookie or OAuth token.

    Cookie-first (production path). ``X-Dev-User`` is a TEST-ONLY
    seam gated behind ``HEXGATE_ALLOW_DEV_USER_HEADER`` — the dashboard
    no longer sends this header from Phase 3d onward; only the test
    suite uses it, via the conftest that flips the env on. Production
    deployments must leave the env unset (the default) so the header
    is silently ignored.

    An OAuth access token is checked after the cookie: a route accepts one
    only if it declares the token's scope, and only for a project the token
    names (see :func:`_oauth_user`).
    """
    # The accepted token's claims, or None: set on every path, so a handler
    # can tell an AI-client call from a dashboard one without a getattr.
    request.state.oauth = None
    if cookie_user is not None:
        return cookie_user

    bearer = _oauth_bearer(authorization)
    if bearer is not None:
        return await _oauth_user(bearer, request, session)

    if x_dev_user and _dev_user_header_allowed():
        user = await session.get(User, x_dev_user)
        if user is not None and user.is_active:
            return user

    raise HTTPException(status_code=401, detail="missing or invalid authentication")


def oauth_projects(request: Request) -> frozenset[str] | None:
    """The projects an OAuth caller's token names, or None for a dashboard caller.

    For the ``OAUTH_NO_PROJECT`` routes, which have no ``project_id`` for
    :func:`require_user` to check and must filter their results by it instead.
    """
    claims = request.state.oauth
    return None if claims is None else claims.projects


def _oauth_bearer(authorization: str | None) -> str | None:
    """The bearer of ``authorization`` if it is an OAuth token, not an SDK key."""
    if authorization is None:
        return None
    scheme, _, bearer = authorization.partition(" ")
    if scheme.lower() != "bearer":  # case-insensitive, RFC 9110 §11.1
        return None
    bearer = bearer.strip()
    return None if bearer.startswith(_SDK_KEY_PREFIX) else bearer


async def _oauth_user(bearer: str, request: Request, session: AsyncSession) -> User:
    """The active user an OAuth access token names, if this route accepts it.

    401 for a token that doesn't verify or whose user is gone or inactive;
    403 for one whose scope or projects don't cover this route. The route's
    own permission checks (org member, project admin) still run after.
    """
    claims = _verify_bearer(bearer)
    user = await session.get(User, claims.sub)
    if user is None or not user.is_active:
        raise _invalid_token()
    # FastAPI puts the matched route in the scope before dependencies run.
    extra = request.scope["route"].openapi_extra or {}
    if not _scopes_cover(extra.get(OAUTH_SCOPE_KEY), claims.scopes):
        raise HTTPException(
            status_code=403,
            detail="token scope does not cover this route",
            headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
        )
    project_id = request.path_params.get("project_id")
    no_project = bool(extra.get(OAUTH_NO_PROJECT))
    if not _project_covered(project_id, no_project, claims.projects):
        raise HTTPException(status_code=403, detail="token does not cover this project")
    _log.info(
        "oauth token accepted: sub=%s client_id=%s jti=%s",
        claims.sub,
        claims.client_id,
        claims.jti,
    )
    request.state.oauth = claims
    return user


def _scopes_cover(needed: str | list[str] | None, granted: frozenset[str]) -> bool:
    """Whether ``granted`` holds the route's scope, or every one of a list of them.

    An unmarked route (``None``) or an empty list is covered by nothing.
    """
    required = {needed} if isinstance(needed, str) else set(needed or ())
    return bool(required) and required <= granted


def _project_covered(
    project_id: str | None, no_project: bool, granted: frozenset[str]
) -> bool:
    """Whether the token names the route's project.

    A route with a ``project_id`` is always checked, tagged or not; the
    ``OAUTH_NO_PROJECT`` tag only excuses a route that has none.
    """
    if project_id is None:
        return no_project
    return project_id in granted


def _verify_bearer(bearer: str) -> AccessTokenClaims:
    settings = get_settings()
    try:
        # The API's own URL, not the MCP resource's: the MCP server forwards the
        # token, so it is minted for both, and a token for the MCP server alone
        # must not pass through to the API.
        return verify_access_token(
            bearer, issuer=settings.public_url, audience=settings.public_url
        )
    except InvalidAccessTokenError as exc:
        # The reason only, never the token.
        _log.info("oauth token refused: %s", exc)
        raise _invalid_token() from None


def _invalid_token() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="missing or invalid authentication",
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )
