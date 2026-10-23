"""What every tool shares: the caller's token, checked for the tool's scope, and
the cap on what a result sends back to the model."""

import json
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp.exceptions import ToolError

from hexgate_mcp.auth import HexgateAccessToken

# A manifest of a large agent fleet is the only realistic hit; past this the
# model gets as many whole list items as fit, or else the head and a marker
# saying the rest was cut.
RESULT_CAP_BYTES = 256 * 1024
TRUNCATED_MARKER = '\n{"truncated": true}'
TRUNCATED_LIST_OPEN = '{"truncated": true, "items": ['


def caller(scope: str) -> HexgateAccessToken:
    """The caller's token, once it carries `scope`. Refused here for a clear
    error before any request; the API is meant to refuse the same token on its
    own (see hexgate_mcp.api)."""
    token = get_access_token()
    if not isinstance(token, HexgateAccessToken):
        raise ToolError("not authenticated")
    if scope not in token.scopes:
        raise ToolError(f"this connection was not granted the {scope!r} scope")
    return token


def result_text(value: Any) -> str:
    """The API's JSON as text, at most RESULT_CAP_BYTES."""
    text = _dumps(value)
    encoded = text.encode()
    if len(encoded) <= RESULT_CAP_BYTES:
        return text
    if isinstance(value, list):
        listed = _whole_items(value)
        if listed is not None:
            return listed
    head = encoded[: RESULT_CAP_BYTES - len(TRUNCATED_MARKER)]
    # A cut inside a multi-byte character drops that character, not the result.
    return head.decode(errors="ignore") + TRUNCATED_MARKER


def _whole_items(value: list[Any]) -> str | None:
    """`{"truncated": true, "items": [...]}` with the leading items that fit,
    so the result still parses and no item is half cut. None when not even the
    first item fits."""
    used = len(TRUNCATED_LIST_OPEN.encode()) + len("]}")
    items: list[str] = []
    for item in value:
        part = _dumps(item)
        size = len(part.encode()) + (len(", ") if items else 0)
        if used + size > RESULT_CAP_BYTES:
            break
        items.append(part)
        used += size
    if not items:
        return None
    return TRUNCATED_LIST_OPEN + ", ".join(items) + "]}"


def _dumps(value: Any) -> str:
    # Unescaped: a project named "Équipe" reads as itself, in a third of the bytes.
    return json.dumps(value, ensure_ascii=False)
