"""The assistant's HTTP app: for now only its liveness route, plus the
database its turns will use, whose tables are created at start-up."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from hexgate_assistant.db import create_engine, create_tables, session_factory
from hexgate_assistant.settings import Settings


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "hexgate-assistant"})


def create_app(settings: Settings) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        try:
            # Before the first request: a database that cannot be reached
            # stops the start-up, rather than every turn failing later.
            await create_tables(engine)
            app.state.db_sessions = session_factory(engine)
            yield
        finally:
            await engine.dispose()

    return Starlette(
        routes=[Route("/v1/assistant/health", health, methods=["GET"])],
        lifespan=lifespan,
    )
