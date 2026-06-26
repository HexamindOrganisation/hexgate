"""Serve the built React dashboard (``dist/``) from the FastAPI app, same-origin.

The production image bakes the dashboard build into itself (see
``platform/api/Dockerfile``) and points ``HEXGATE_DASHBOARD_DIST`` at it, so the
API answers both ``/v1/*`` and the SPA on one origin — no separate edge
container, no CORS, no cross-origin cookie config. The same helper backs the
single-tenant demo bundle (:mod:`demo`).

:func:`mount_spa` MUST be called *after* ``app.include_router(v1)`` so the
``/v1`` API + WebSocket routes match before the catch-all SPA fallback —
Starlette matches in registration order, so registering this last is what keeps
``/v1/*`` from being shadowed by ``index.html``.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def dashboard_dist() -> Path:
    """Resolve the built dashboard directory.

    Defaults to ``platform/dashboard/dist`` relative to this file (the layout
    after ``pnpm build`` in a dev checkout); the production image overrides it
    with ``HEXGATE_DASHBOARD_DIST`` since the build is copied to ``/app/static``.
    """
    override = os.environ.get("HEXGATE_DASHBOARD_DIST", "").strip()
    if override:
        return Path(override)
    return Path(__file__).parent.parent / "dashboard" / "dist"


def mount_spa(app: FastAPI) -> None:
    """Attach static SPA serving (assets mount + catch-all → index.html) to ``app``.

    Idempotent in intent but registers routes, so call exactly once, last.
    """
    dist = dashboard_dist()
    if not (dist / "index.html").is_file():
        # Build missing — don't crash the API; /v1 still works, the SPA 404s
        # with a clear hint instead of an opaque mount error at import time.
        @app.get("/{_full_path:path}", include_in_schema=False)
        async def _no_build(_full_path: str) -> Response:
            return Response(
                f"dashboard build not found at {dist} — run `pnpm build` in "
                "platform/dashboard or set HEXGATE_DASHBOARD_DIST",
                status_code=404,
                media_type="text/plain",
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
