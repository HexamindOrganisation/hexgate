"""What every tool shares: the caller's token, checked for the tool's scope, and
the cap on what a result sends back to the model."""

import json
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp.exceptions import ToolError

from hexgate_mcp.auth import HexgateAccessToken

# A manifest of a large agent fleet is the only realistic hit; past this the
# model gets the head and a marker saying the rest was cut.
RESULT_CAP_BYTES = 256 * 1024
TRUNCATED_MARKER = '\n{"truncated": true}'


def caller(*scopes: str, project_id: str | None = None) -> HexgateAccessToken:
    """The caller's token, once it carries every one of `scopes` and, when the
    tool names a project, that project. Refused here for a clear error before
    any request; the API refuses the same token on its own."""
    token = get_access_token()
    if not isinstance(token, HexgateAccessToken):
        raise ToolError("not authenticated")
    for scope in scopes:
        if scope not in token.scopes:
            raise ToolError(f"this connection was not granted the {scope!r} scope")
    if project_id is not None and project_id not in token.projects:
        raise ToolError(
            f"project {project_id!r} is not one of the projects granted to this "
            "connection; call projects_list for their ids"
        )
    return token


def result_text(value: Any) -> str:
    """The API's JSON as text, cut to RESULT_CAP_BYTES with the head kept."""
    # Unescaped: a project named "Équipe" reads as itself, in a third of the bytes.
    text = json.dumps(value, ensure_ascii=False)
    encoded = text.encode()
    if len(encoded) <= RESULT_CAP_BYTES:
        return text
    head = encoded[: RESULT_CAP_BYTES - len(TRUNCATED_MARKER)]
    # A cut inside a multi-byte character drops that character, not the result.
    return head.decode(errors="ignore") + TRUNCATED_MARKER
