"""`projects_list`: how a client finds the `project_id` every other tool takes."""

import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from hexgate_mcp.api import ApiClient, ApiRefusal
from hexgate_mcp.tools import caller, result_text


def register(mcp: FastMCP, api: ApiClient) -> None:
    @mcp.tool(
        # Only reads: clients may call it without asking the user first.
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        # Unstructured: a `str` return otherwise goes out a second time as
        # structuredContent, and the result cap would bound only one copy.
        structured_output=False,
    )
    async def projects_list() -> str:
        """List the Hexgate projects this connection may use, with their ids.
        Call it first: every other tool takes a `project_id` from this list."""
        token = caller("projects:read")
        found = await asyncio.gather(
            *(_granted_project(api, p, token.token) for p in token.projects)
        )
        projects = [p for p in found if not isinstance(p, ApiRefusal)]
        if not projects:
            # Every one refused is not "no projects": it is the API refusing
            # this token or route (or a wrong HEXGATE_MCP_API_URL), so say so.
            raise found[0]
        return result_text(projects)


async def _granted_project(api: ApiClient, project_id: str, token: str) -> Any:
    """One project ticked at consent, or the API's refusal when it is gone
    (deleted, or the user left its org), so the others are still listed."""
    try:
        return await api.get(f"/projects/{project_id}", token)
    except ApiRefusal as refusal:
        if refusal.status in (403, 404):
            return refusal
        raise
