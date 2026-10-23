"""`policy_save`: the one write over the API, with the dashboard's permissions."""

from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from hexgate_mcp.api import ApiClient
from hexgate_mcp.tools import caller, result_text, without_content


def file_path(project_id: str, name: str) -> str:
    """The route for `name`, refused when a segment would move the request.
    httpx resolves `.` and `..` in a URL path, so `../agents/x` would leave
    the policy-files route for another one of the project's."""
    if any(segment in ("", ".", "..") for segment in name.split("/")):
        raise ToolError(
            f"invalid policy file name {name!r}: use a relative path such as "
            "'policy.yaml' or 'modules/billing.yaml', with no '.' or '..'"
        )
    project = quote(project_id, safe="")
    return f"/projects/{project}/policy-files/{quote(name, safe='/')}"


def register(mcp: FastMCP, api: ApiClient) -> None:
    @mcp.tool(
        # Replaces the stored file, and saving the same content twice is a no-op.
        annotations=ToolAnnotations(
            destructiveHint=True, idempotentHint=True, openWorldHint=False
        ),
        structured_output=False,  # as in projects.py
    )
    async def policy_save(project_id: str, name: str, content: str) -> str:
        """Create or replace one file of the project's policy: `policy.yaml` or
        a file it imports, such as `modules/billing.yaml`. Needs project admin
        rights. Nothing is saved when the content is not a valid policy file
        (422) or when the project would no longer resolve with it (409); the
        error says why. A project with no `policy.yaml` enforces either each
        agent's own policy or its role bindings; saving the first one replaces
        that with the composed files, so ask the user first. Run policy_check
        on the draft first."""
        token = caller("policy:write", project_id=project_id)
        path = file_path(project_id, name)
        # The saved file without its text, which the model just sent.
        saved = await api.put(path, token.token, {"content": content})
        return result_text(without_content(saved))
