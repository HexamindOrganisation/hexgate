"""The assistant's HTTP app: for now only its liveness route."""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "hexgate-assistant"})


def create_app() -> Starlette:
    return Starlette(routes=[Route("/v1/assistant/health", health, methods=["GET"])])
