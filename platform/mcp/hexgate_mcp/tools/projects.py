"""`projects_list`: how a client finds the `project_id` every other tool takes."""

from mcp.server.fastmcp import FastMCP

from hexgate_mcp.api import ApiClient
from hexgate_mcp.tools import caller, result_text


def register(mcp: FastMCP, api: ApiClient) -> None:
    # Unstructured: a `str` return otherwise goes out a second time as
    # structuredContent, and the result cap would bound only one copy.
    @mcp.tool(structured_output=False)
    async def projects_list() -> str:
        """List the Hexgate projects this connection may use, with their ids.
        Call it first: every other tool takes a `project_id` from this list."""
        token = caller("projects:read")
        projects = []
        for org in await api.get("/orgs", token.token):
            projects += await api.get(f"/orgs/{org['id']}/projects", token.token)
        # The user's other projects stay out: the grant covers only the ones
        # ticked at consent, and every other tool would refuse them.
        return result_text([p for p in projects if p["id"] in token.projects])
