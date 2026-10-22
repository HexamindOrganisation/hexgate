"""Calls the platform API as the caller: their bearer token, forwarded unchanged.

That is not token passthrough: the API is to mint the token with both
audiences, this server's resource URL and its own public URL (spec §I, with
plan PR 7), so the API verifies it as a token issued for itself. That check, once plan PR 9 (#403)
and plan PR 10 land, is the security boundary; until then the API accepts only
its session cookie and answers this bearer with 401. This client only turns
the API's answers into tool errors the model can read.
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
# fix the request. Anything else is the API failing (see `_send`).
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

    async def get(self, path: str, token: str, params: dict | None = None) -> Any:
        # httpx sends a None value as an empty string; an unset optional
        # parameter is left out instead, so the API applies its own default.
        query = {k: v for k, v in (params or {}).items() if v is not None}
        return await self._send("GET", path, token, params=query)

    async def _send(self, method: str, path: str, token: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(
                method,
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise _api_failed(method, path, type(exc).__name__) from exc
        _raise_refusal(response)
        if not response.is_success:
            raise _api_failed(method, path, str(response.status_code))
        try:
            return response.json()
        except ValueError as exc:
            raise _api_failed(method, path, "response is not JSON") from exc


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


def _api_failed(method: str, path: str, cause: str) -> ToolError:
    """Logged here, since FastMCP turns a tool's exception into an error result
    without a log line. The model gets no internal URL: httpx's own messages
    name the private-network API host."""
    logger.warning("API call failed: %s %s: %s", method, path, cause)
    return ToolError("the Hexgate API failed to answer; try again shortly")


def _detail(response: httpx.Response) -> str:
    """FastAPI's `detail`: a string, or a list of field errors on a 422."""
    try:
        detail = response.json()["detail"]
    except (ValueError, KeyError, TypeError):
        return response.text[:DETAIL_CAP_CHARS]
    return detail if isinstance(detail, str) else json.dumps(detail)
