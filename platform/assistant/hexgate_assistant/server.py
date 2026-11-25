"""The assistant's HTTP app: its liveness route and the turn route, over the
database its turns use, whose tables are created at start-up."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from hexgate_assistant.agent import build_agent
from hexgate_assistant.auth import JwksTokenVerifier
from hexgate_assistant.context import context_budget
from hexgate_assistant.db import (
    create_tables,
    make_engine,
    session_factory,
    verify_schema,
)
from hexgate_assistant.jwks import JwksCache
from hexgate_assistant.model import build_model
from hexgate_assistant.router import post_turn
from hexgate_assistant.runner import Turns
from hexgate_assistant.settings import Settings

# A stop, in this order, within the 10 s Docker allows (compose sets no
# stop_grace_period): open streams get GRACEFUL_SECONDS before they are cut,
# which is what lets the shutdown below start; then the turns still running
# get SHUTDOWN_SECONDS to store what they got to (runner.STORE_SECONDS) and
# release their locks: about 8 s with the process's own exit. Past Docker's
# 10 s the process is killed and a lock waits out its stale time instead.
GRACEFUL_SECONDS = 3
SHUTDOWN_SECONDS = 5


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "hexgate-assistant"})


def create_app(settings: Settings, *, model: Model | None = None) -> Starlette:
    """The app. `model` replaces the one `settings.model` names, in tests."""
    if model is None and settings.model is not None:
        model = build_model(settings.model, settings.openrouter_key)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        try:
            # Before the first request: a database that cannot be reached,
            # or that lacks a column the code reads, stops the start-up,
            # rather than every turn failing later.
            await create_tables(engine)
            await verify_schema(engine)
            db = session_factory(engine)
            async with httpx.AsyncClient(timeout=10) as http:
                app.state.verifier = JwksTokenVerifier(
                    keys=JwksCache(settings.jwks_url, http),
                    issuer=settings.issuer,
                    audience=settings.resource_url,
                )
                # No model configured: every turn is refused.
                app.state.turns = None if model is None else _turns(settings, model, db)
                try:
                    yield
                finally:
                    if app.state.turns is not None:
                        await app.state.turns.stop(SHUTDOWN_SECONDS)
        finally:
            await engine.dispose()

    return Starlette(
        routes=[
            Route("/v1/assistant/health", health, methods=["GET"]),
            Route(
                "/v1/assistant/projects/{project_id}/turns", post_turn, methods=["POST"]
            ),
        ],
        lifespan=lifespan,
    )


def _turns(
    settings: Settings, model: Model, db: async_sessionmaker[AsyncSession]
) -> Turns:
    return Turns(
        db=db,
        agent=build_agent(model, settings.mcp_url),
        mcp_url=settings.mcp_url,
        budget=context_budget(model.context_window, settings.context_tokens),
    )
