"""Calls the platform API as the caller: their bearer token, forwarded unchanged.

That is not token passthrough: the API is to mint the token for two
audiences, this server's resource URL and its own public URL, so it verifies
the token as its own; nothing mints one until the code exchange lands. The
API's bearer branch and its per-route scope marks, already in place, are the
security boundary. This client only turns the API's answers into tool errors
the model can read.
"""

import json
import logging
from typing import Any

import httpx
from mcp.server.fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)

# A refusal body that is not FastAPI's JSON (an HTML error page) is cut to
# this many characters before it reaches the model.
DETAIL_CAP_CHARS = 300

# The refusals a caller can act on: log in again, ask for a scope or project,
# fix the request. Anything else is the API failing (see `get`).
TOOL_ERRORS = {
    401: "not authenticated",
    403: "forbidden",
    404: "not found",
    409: "conflict",
    422: "invalid request",
}


class ApiClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self._base_url = base_url
        self._http = http

    async def get(self, path: str, token: str) -> Any:
        try:
            response = await self._http.get(
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise _api_failed(path, type(exc).__name__) from exc
        _raise_refusal(response)
        if not response.is_success:
            raise _api_failed(path, str(response.status_code))
        try:
            return response.json()
        except ValueError as exc:
            raise _api_failed(path, "response is not JSON") from exc


class ApiRefusal(ToolError):
    """A TOOL_ERRORS status, kept on the error so a tool can tell them apart."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _raise_refusal(response: httpx.Response) -> None:
    reason = TOOL_ERRORS.get(response.status_code)
    if reason is not None:
        raise ApiRefusal(
            response.status_code,
            f"{response.status_code} {reason}: {_detail(response)}",
        )


def _api_failed(path: str, cause: str) -> ToolError:
    """Logged here, since FastMCP turns a tool's exception into an error result
    without a log line. The model gets no internal URL: httpx's own messages
    name the private-network API host."""
    logger.warning("API call failed: GET %s: %s", path, cause)
    return ToolError("the Hexgate API failed to answer; try again shortly")


def _detail(response: httpx.Response) -> str:
    """FastAPI's `detail`: a string, or a list of field errors on a 422."""
    try:
        detail = response.json()["detail"]
    except (ValueError, KeyError, TypeError):
        return response.text[:DETAIL_CAP_CHARS]
    return detail if isinstance(detail, str) else json.dumps(detail)
