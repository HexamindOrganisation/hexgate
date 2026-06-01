"""FastAPI Users wiring — registration, login, cookie sessions.

This module owns the human-side auth surface: ``POST /v1/auth/register``,
cookie login/logout, password reset (Phase 3b later), and the
``current_active_user`` / ``current_active_user_optional`` dependencies
the rest of the route table consumes.

The two auth surfaces (humans via cookie, machines via biscuit) stay
separate by design — biscuits live in ``main._validate_sdk_token`` and
``require_project``; this module never touches them. See
``m3-platform-auth.md`` for the dual-surface rationale.

ID type is ``str`` (UUID-formatted) rather than ``uuid.UUID`` so the
column types stay aligned with the rest of our str-keyed SQLModel
tables. FastAPI Users is generic over the ID type — both
``SQLAlchemyUserDatabase`` and ``FastAPIUsers`` accept the parametrised
form ``[User, str]`` without any extra adapter code.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Depends, Request
from fastapi_users import BaseUserManager, FastAPIUsers, schemas
from fastapi_users.authentication import (
    AuthenticationBackend,
    CookieTransport,
    JWTStrategy,
)
from fastapi_users.db import SQLAlchemyUserDatabase
from sqlmodel.ext.asyncio.session import AsyncSession

from db import get_session
from models import User

logger = logging.getLogger("fortify.platform.auth")


# ---------------------------------------------------------------------------
# Pydantic schemas — wire shapes for register / read / update
# ---------------------------------------------------------------------------


class UserRead(schemas.BaseUser[str]):
    """What clients see on ``GET /v1/users/me`` and the register response.

    Inherits ``id`` / ``email`` / ``is_active`` / ``is_superuser`` /
    ``is_verified`` from the FastAPI Users base. ``hashed_password`` is
    intentionally NOT exposed — schemas.BaseUser excludes it.
    """


class UserCreate(schemas.BaseUserCreate):
    """Body of ``POST /v1/auth/register`` — just ``email`` + ``password``.

    Extra fields the model carries (``is_active``, ``is_superuser``) are
    server-controlled and ignored if a client tries to send them.
    """


class UserUpdate(schemas.BaseUserUpdate):
    """Body of ``PATCH /v1/users/me`` — limited self-service updates."""


# ---------------------------------------------------------------------------
# Database adapter + user manager
# ---------------------------------------------------------------------------


async def get_user_db(
    session: AsyncSession = Depends(get_session),
) -> AsyncGenerator[SQLAlchemyUserDatabase, None]:
    """Yield the FastAPI Users SQLAlchemy adapter bound to a per-request session.

    Same ``get_session`` every other route handler uses — one async
    session per request, scoped to the dependency-injection lifecycle.
    """
    yield SQLAlchemyUserDatabase(session, User)


def _session_secret() -> str:
    """Derive a stable session-JWT secret from the platform's signing key.

    Lazy import of ``main.keystore`` to dodge the import cycle (main.py
    imports auth.py for the routers). Domain-separation prefix keeps
    this distinct from any other key derivation off the same root.
    Rotating the keystore invalidates every session — that's the right
    blast radius.
    """
    from main import keystore

    return hashlib.sha256(
        b"hexagate-session-v1:" + keystore._private_key_bytes()
    ).hexdigest()


_SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days; rolling refresh comes later


class UserManager(BaseUserManager[User, str]):
    """User lifecycle hooks (register / login / password reset).

    Phase 3a wires the structural pieces but leaves the email-sending
    hooks as info-level logs — Phase 3b plugs Resend in. ``parse_id``
    is the only protocol method we MUST override for str IDs (the
    default expects uuid.UUID).
    """

    # The token secrets sign reset / verify links. Lazy via @property
    # so the keystore can be patched in tests without restamping these
    # at module import time.
    @property
    def reset_password_token_secret(self) -> str:  # type: ignore[override]
        return _session_secret()

    @property
    def verification_token_secret(self) -> str:  # type: ignore[override]
        return _session_secret()

    def parse_id(self, value: Any) -> str:
        """ID is a str. FastAPI Users calls this on path-string IDs."""
        return str(value)

    async def on_after_register(
        self, user: User, request: Request | None = None
    ) -> None:
        """Hook after a successful ``POST /v1/auth/register``.

        Phase 3b will send a verification email here. For now we just
        log — operators can confirm registration succeeded by reading
        the platform's stderr.
        """
        logger.info("user registered: %s (id=%s)", user.email, user.id)

    async def on_after_forgot_password(
        self, user: User, token: str, request: Request | None = None
    ) -> None:
        """Hook when a password reset is requested.

        Phase 3b sends a real email; for now print the token to stderr
        so dev can copy the link and exercise the reset flow without
        SMTP set up.
        """
        logger.info(
            "password reset requested for %s — token: %s", user.email, token
        )

    async def on_after_request_verify(
        self, user: User, token: str, request: Request | None = None
    ) -> None:
        """Hook when a verification email is requested. Same shape as above."""
        logger.info(
            "verification requested for %s — token: %s", user.email, token
        )


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase = Depends(get_user_db),
) -> AsyncGenerator[UserManager, None]:
    yield UserManager(user_db)


# ---------------------------------------------------------------------------
# Authentication backend — cookie transport + JWT strategy
# ---------------------------------------------------------------------------


cookie_transport = CookieTransport(
    cookie_name="fortify_session",
    cookie_max_age=_SESSION_TTL_SECONDS,
    # ``secure=True`` would refuse the cookie over plain HTTP — fine in
    # prod (HTTPS terminator in front), wrong for ``make platform-api``
    # which serves on localhost over HTTP. Toggle via env when we deploy.
    cookie_secure=False,
    cookie_httponly=True,
    # ``lax`` — strict breaks OAuth redirects when we add Google sign-in
    # (Phase 3c). The OWASP recommendation for general use is lax.
    cookie_samesite="lax",
)


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=_session_secret(), lifetime_seconds=_SESSION_TTL_SECONDS)


auth_backend = AuthenticationBackend(
    name="cookie",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)


# ---------------------------------------------------------------------------
# FastAPIUsers instance + the dependencies the rest of the API consumes
# ---------------------------------------------------------------------------


fastapi_users = FastAPIUsers[User, str](
    get_user_manager,
    [auth_backend],
)


# Returns the current ``User`` row or raises 401. Used by ``GET /v1/users/me``
# and (Phase 3a) consulted by ``require_user`` before the X-Dev-User fallback.
current_active_user = fastapi_users.current_user(active=True)

# Returns the current ``User`` or ``None`` (no raise) — the variant
# ``require_user`` actually uses, so it can fall back to X-Dev-User
# during the transition without a try/except.
current_active_user_optional = fastapi_users.current_user(active=True, optional=True)
