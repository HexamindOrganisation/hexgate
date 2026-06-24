"""Single-tenant demo bundle: serve the built dashboard same-origin + auto-login.

Wired into :mod:`main` only when ``HEXGATE_DEMO`` is truthy, so normal
``make platform-api`` / production runs are completely unaffected. The goal
is a throwaway, per-visitor container where:

  * the FastAPI app *also* serves the built React dashboard (``dist/``) from
    the same origin — so the ``hexgate_session`` cookie and the ``/v1`` WebSocket
    resolve without any CORS / cross-origin cookie config, behind a single
    public URL (e.g. one ``modal.forward(8000)`` tunnel); and

  * ``GET /v1/demo-login`` logs the seeded default user in (sets the session
    cookie) and redirects to the playground, so the visitor lands signed-in
    with no login wall. There is exactly one org / user / project in the
    container's fresh SQLite (created by ``ensure_default_seed`` at startup),
    so "log in as the default user" is unambiguous.

Container dies → SQLite + the ephemeral org/project evaporate. Nothing to
garbage-collect in a shared database.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles


def _dashboard_dist() -> Path:
    """Resolve the built dashboard directory.

    Defaults to ``platform/dashboard/dist`` relative to this file; override
    with ``HEXGATE_DASHBOARD_DIST`` for non-standard layouts (e.g. the build
    copied elsewhere in a container image).
    """
    override = os.environ.get("HEXGATE_DASHBOARD_DIST", "").strip()
    if override:
        return Path(override)
    return Path(__file__).parent.parent / "dashboard" / "dist"


def enable_demo(app: FastAPI) -> None:
    """Attach the demo-login route + SPA static serving to ``app``.

    MUST be called *after* ``app.include_router(v1)`` so the ``/v1`` API and
    WebSocket routes are matched before the catch-all SPA fallback. Starlette
    matches routes in registration order, so registering this last is what
    keeps ``/v1/*`` from being shadowed by ``index.html``.
    """

    # ----- auto-login -----------------------------------------------------
    @app.get("/v1/demo-login", include_in_schema=False)
    async def demo_login() -> Response:
        """Mint a session cookie for the seeded default user and land on /playground.

        Reuses the platform's real auth backend (JWT strategy + the exact
        cookie attributes from ``auth.cookie_transport``) so the issued cookie
        is indistinguishable from a normal password login — the dashboard's
        ``ws_require_org_member`` accepts it unchanged.
        """
        from auth import _SESSION_TTL_SECONDS, _cookie_secure, get_jwt_strategy
        from db import async_session_factory
        from models import User
        from services import DEFAULT_USER_ID

        async with async_session_factory() as session:
            user = await session.get(User, DEFAULT_USER_ID)
            if user is None:  # seed disabled (HEXGATE_SEED=skip) — nothing to log into
                return RedirectResponse(url="/", status_code=303)
            token = await get_jwt_strategy().write_token(user)

        resp = RedirectResponse(url="/playground", status_code=303)
        resp.set_cookie(
            key="hexgate_session",
            value=token,
            max_age=_SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(),  # HEXGATE_COOKIE_SECURE=1 over the https tunnel
        )
        return resp

    # ----- static SPA -----------------------------------------------------
    dist = _dashboard_dist()
    if not (dist / "index.html").is_file():
        # Build missing — don't crash the API; /v1 still works, the SPA 404s
        # with a clear hint instead of an opaque mount error at import time.
        @app.get("/{_full_path:path}", include_in_schema=False)
        async def _no_build(_full_path: str) -> Response:
            raise RuntimeError(
                f"dashboard build not found at {dist} — run `pnpm build` in "
                "platform/dashboard or set HEXGATE_DASHBOARD_DIST"
            )

        return

    # Hashed assets get a real static mount (correct content-type + caching);
    # everything else falls through to the SPA catch-all → index.html, so
    # client-side routes like /playground resolve on a hard refresh.
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    dist_resolved = dist.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Response:
        candidate = (dist / full_path).resolve()
        # Resolve first, then containment-check, so `../` traversal can't escape dist.
        if (
            full_path
            and candidate.is_file()
            and candidate.is_relative_to(dist_resolved)
        ):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")
