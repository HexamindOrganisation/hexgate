"""`policy_apply`: check a change to the policy files and save it in one call."""

from typing import Literal
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from hexgate_mcp.api import ApiClient
from hexgate_mcp.tools import caller, result_text

# The apply checks every file, runs the samples and builds each agent's bundle
# before it commits, which the shared client's 5 s does not allow for.
APPLY_TIMEOUT_SECONDS = 30.0


class ApplyFile(BaseModel):
    """A whole file to save. `base_hash` is the `text_hash` of the stored file
    the change was written against; null when the file does not exist yet."""

    # Refused here: the default would drop a misspelt key before the API,
    # whose own `extra="forbid"` then never sees it.
    model_config = ConfigDict(extra="forbid")

    name: str
    content: str
    base_hash: str | None


class ApplySample(BaseModel):
    """A tool call and the outcome it must get once the change is saved."""

    model_config = ConfigDict(extra="forbid")  # as ApplyFile

    agent: str = "*"
    role: str
    tool: str
    args: dict = {}
    attributes: dict = {}
    expect: Literal["allow", "deny", "approval_required"]


def register(mcp: FastMCP, api: ApiClient) -> None:
    @mcp.tool(
        # Replaces the stored files, but applying the same change twice is
        # refused (409 stale), not repeated.
        annotations=ToolAnnotations(
            destructiveHint=True, idempotentHint=False, openWorldHint=False
        ),
        structured_output=False,  # as in projects.py
    )
    async def policy_apply(
        project_id: str,
        files: list[ApplyFile],
        summary: str,
        samples: list[ApplySample] | None = None,
        session_id: str | None = None,
    ) -> str:
        """Check a change to the project's policy files and, when every check
        passes, save it all at once: 1 to 10 whole files, each added or
        replaced (never deleted). Needs project admin rights. Give each
        file's `base_hash`, the `text_hash` policy_read_file returned for it,
        or null for a file that does not exist yet. `summary` says what the
        change is for; `samples` (up to 20) are tool calls that must get the
        `expect`ed outcome once saved. Run policy_check first.
        `applied: true` means the change is saved; `applied: false` means a
        check failed and nothing was saved, `checks` says which. Errors: 409
        `stale` lists the files that changed since you read them, so read
        them again and rebuild the change; 409 `layout` means the project has
        no `policy.yaml`; 403 `assistant_mode` means the organization does not
        let the assistant write. If the call times out or fails with no
        answer, the change may be saved: read the files again before
        retrying, since a retry then gets 409 `stale`."""
        token = caller("policy:apply", project_id=project_id)
        body = {
            "files": [f.model_dump() for f in files],
            "summary": summary,
            "samples": [s.model_dump() for s in samples or []],
            "session_id": session_id,
        }
        path = f"/projects/{quote(project_id, safe='')}/policy/apply"
        applied = await api.post(path, token.token, body, timeout=APPLY_TIMEOUT_SECONDS)
        return result_text(applied)
