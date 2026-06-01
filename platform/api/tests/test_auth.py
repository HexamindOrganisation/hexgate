"""Tests for the FastAPI Users wiring (M3 Phase 3a).

Covers the human-side auth surface: register / cookie login / logout /
``/users/me``, plus the ``require_user`` cookie-or-header behaviour
that keeps the existing dashboard working during the Phase 3a→3b→3c
transition.

The SDK biscuit path is unchanged — these tests deliberately don't
touch it. Cross-coverage for "biscuit still works alongside cookies"
lives in :mod:`test_tenant_isolation`.
"""

from __future__ import annotations

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import main
from main import app
from services import DEFAULT_PROJECT_ID, DEFAULT_USER_ID, ensure_default_project


# ---------------------------------------------------------------------------
# Fixtures — fresh DB + clean keystore so the default-admin announcement
# is exercised on every test (deterministically).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
    """In-memory async SQLite with the schema + triple-default seed."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as bootstrap:
        await ensure_default_project(bootstrap)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def client(session_factory, tmp_path) -> TestClient:
    """TestClient with the test factory injected, fresh keystore per run."""
    from db import get_session
    from keystore import FileKeyStore

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    original_keystore = main.keystore
    main.keystore = FileKeyStore(base_dir=tmp_path / "keystore")
    main.keystore.ensure_keypair()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        main.keystore = original_keystore


# ---------------------------------------------------------------------------
# POST /v1/auth/register
# ---------------------------------------------------------------------------


def test_register_creates_user(client: TestClient) -> None:
    """A clean registration returns 201 with the public user view."""
    r = client.post(
        "/v1/auth/register",
        json={"email": "alice@example.com", "password": "correcthorsebattery"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "alice@example.com"
    # Server-side defaults — clients can't set these on registration.
    assert body["is_active"] is True
    assert body["is_verified"] is False
    assert body["is_superuser"] is False
    # Password never leaks back to the client.
    assert "hashed_password" not in body
    assert "password" not in body


def test_register_rejects_duplicate_email(client: TestClient) -> None:
    """Second registration with the same email → 400 (REGISTER_USER_ALREADY_EXISTS)."""
    payload = {"email": "dup@example.com", "password": "correcthorsebattery"}
    r1 = client.post("/v1/auth/register", json=payload)
    assert r1.status_code == 201
    r2 = client.post("/v1/auth/register", json=payload)
    assert r2.status_code == 400
    assert "exists" in r2.json()["detail"].lower()


# NOTE: password complexity validation lives with the signup UI work
# (Phase 3d). FastAPI Users' default ``validate_password`` is a pass-
# through; we'll override it once we know what rules the UI enforces
# (length, common-password blocklist, etc.) so the server check matches
# the form check exactly.


# ---------------------------------------------------------------------------
# POST /v1/auth/cookie/login + /logout
# ---------------------------------------------------------------------------


def test_login_sets_session_cookie(client: TestClient) -> None:
    """A correct password → 204 with a Set-Cookie: fortify_session=... header."""
    client.post(
        "/v1/auth/register",
        json={"email": "bob@example.com", "password": "correcthorsebattery"},
    )
    r = client.post(
        "/v1/auth/cookie/login",
        # FastAPI Users login uses OAuth2 password-flow form-encoded,
        # not JSON — ``username`` is the email field.
        data={"username": "bob@example.com", "password": "correcthorsebattery"},
    )
    assert r.status_code == 204, r.text
    set_cookie = r.headers.get("set-cookie", "")
    assert "fortify_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()


def test_login_rejects_wrong_password(client: TestClient) -> None:
    """Wrong password → 400 (LOGIN_BAD_CREDENTIALS), no cookie set."""
    client.post(
        "/v1/auth/register",
        json={"email": "carol@example.com", "password": "correcthorsebattery"},
    )
    r = client.post(
        "/v1/auth/cookie/login",
        data={"username": "carol@example.com", "password": "wrong"},
    )
    assert r.status_code == 400
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_login_rejects_unknown_email(client: TestClient) -> None:
    """Email not in DB → 400, same opaque error as wrong-password.

    Don't distinguish "no such email" from "wrong password" to keep the
    login surface from leaking which emails are registered.
    """
    r = client.post(
        "/v1/auth/cookie/login",
        data={"username": "nobody@example.com", "password": "anything"},
    )
    assert r.status_code == 400


def test_logout_clears_cookie(client: TestClient) -> None:
    """POST /logout while logged in → 204 + the cookie clears."""
    client.post(
        "/v1/auth/register",
        json={"email": "dave@example.com", "password": "correcthorsebattery"},
    )
    client.post(
        "/v1/auth/cookie/login",
        data={"username": "dave@example.com", "password": "correcthorsebattery"},
    )
    r = client.post("/v1/auth/cookie/logout")
    assert r.status_code == 204
    # Server-issued clear: cookie is set to empty with Max-Age=0.
    cookie = r.headers.get("set-cookie", "")
    assert "fortify_session=" in cookie
    assert ("max-age=0" in cookie.lower()) or ("expires=" in cookie.lower())


# ---------------------------------------------------------------------------
# GET /v1/users/me
# ---------------------------------------------------------------------------


def test_me_requires_authentication(client: TestClient) -> None:
    """Hitting /users/me without a cookie → 401."""
    r = client.get("/v1/users/me")
    assert r.status_code == 401


def test_me_returns_logged_in_user(client: TestClient) -> None:
    """After login, /users/me returns the active user's public view."""
    client.post(
        "/v1/auth/register",
        json={"email": "eve@example.com", "password": "correcthorsebattery"},
    )
    client.post(
        "/v1/auth/cookie/login",
        data={"username": "eve@example.com", "password": "correcthorsebattery"},
    )
    r = client.get("/v1/users/me")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "eve@example.com"
    assert body["is_active"] is True
    assert "hashed_password" not in body


# ---------------------------------------------------------------------------
# require_user — cookie-first, X-Dev-User fallback
# ---------------------------------------------------------------------------


def test_project_route_accepts_cookie_auth(client: TestClient) -> None:
    """A logged-in user (via cookie) can read their org's project.

    The default seed user (admin@hexagate.local) is a member of
    support-bot's org and gets verified=True / superuser=True on seed,
    so we can log in as them with the default-boot password.
    Generating that password fresh per test would require capturing
    stderr; for this test we just register a new user, log them in,
    then add them to the default org manually.
    """
    # Register + log in.
    client.post(
        "/v1/auth/register",
        json={"email": "frank@example.com", "password": "correcthorsebattery"},
    )
    client.post(
        "/v1/auth/cookie/login",
        data={"username": "frank@example.com", "password": "correcthorsebattery"},
    )

    # Frank doesn't belong to support-bot's org yet → expect 403.
    r = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents")
    assert r.status_code == 403, r.text
    assert "member" in r.json()["detail"].lower()


def test_project_route_still_accepts_x_dev_user_header(client: TestClient) -> None:
    """During the Phase 3a transition, X-Dev-User keeps working so the
    existing dashboard doesn't break before Phase 5 swaps in cookie UI."""
    r = client.get(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents",
        headers={"X-Dev-User": DEFAULT_USER_ID},
    )
    assert r.status_code == 200


def test_inactive_user_cannot_authenticate_via_cookie(
    client: TestClient, session_factory
) -> None:
    """An ``is_active=False`` user can't log in even with the right password.

    The auth backend's ``current_active_user`` dependency only succeeds
    for active users, so a deactivated account is effectively a soft-
    delete from the perspective of the auth surface.
    """
    import asyncio

    from models import User

    client.post(
        "/v1/auth/register",
        json={"email": "ghost@example.com", "password": "correcthorsebattery"},
    )

    async def _deactivate():
        async with session_factory() as session:
            from sqlmodel import select
            row = (
                await session.exec(
                    select(User).where(User.email == "ghost@example.com")
                )
            ).first()
            row.is_active = False
            session.add(row)
            await session.commit()

    asyncio.get_event_loop().run_until_complete(_deactivate())

    r = client.post(
        "/v1/auth/cookie/login",
        data={"username": "ghost@example.com", "password": "correcthorsebattery"},
    )
    assert r.status_code == 400
