"""`apply_change`: the model's one write tool.

The model sends whole files, a summary and samples; our code adds each
file's base hash from the turn-start snapshot and calls the MCP server's
`policy_apply`, which runs every platform check and saves only if all of
them pass. The model is never offered `policy_apply` itself, so the limits
below cannot be stepped around.

Per turn: at most `MAX_ATTEMPTS` calls and one applied change. A stale
answer (a file changed since the turn began) or an answer that never came
ends writing for the turn, since a retry would send the same hashes.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict
from pydantic_ai import RunContext, Tool
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)

from hexgate_assistant.context import call_text, cap_tool_result
from hexgate_assistant.deps import TurnDeps, TurnWrites

logger = logging.getLogger(__name__)

TOOL_NAME = "apply_change"
READ_TOOL = "policy_read_file"
APPLY_SCOPE = "policy:apply"
MAX_ATTEMPTS = 3

# The MCP tool's own limit on its call to the API (copied from
# hexgate_mcp/tools/policy_apply.py, since this package never imports it).
APPLY_TIMEOUT_SECONDS = 30.0
# Ours on the MCP call, above the server's: the server's limit must end
# first, so a slow apply comes back as its "failed to answer" while the API
# may still commit. Ours ending first, or at the same moment, would report a
# change the API then saves as a plain failure. The margin covers the
# server's connect phase, which httpx times apart from the read.
APPLY_CALL_SECONDS = APPLY_TIMEOUT_SECONDS + 15

NO_SCOPE = (
    "refused, nothing saved: this turn may not write. Show the user the "
    "change and its YAML instead; they can apply it themselves."
)
ALREADY_APPLIED = (
    "refused: change {change_id} was already applied this turn, and a turn "
    "applies at most one. Tell the user what was saved; they can ask for "
    "more in their next message."
)
ATTEMPT_LIMIT = (
    f"attempt limit reached: {MAX_ATTEMPTS} attempts this turn. Stop and "
    "tell the user what failed."
)
READ_FIRST = (
    "refused, nothing saved: {files}: changed since you last read it, or "
    "not read before this call. Read each with policy_read_file, then "
    "rebuild the change from what it returns."
)
CONFLICT = "Do not call apply_change again this turn; tell the user why."
STALE = (
    "refused, nothing saved: these files changed since this turn began: "
    "{files}. Do not call apply_change again this turn; tell the user to "
    "send their message again, so the next turn reads the files as they are now."
)
# What the MCP server answers when the API did not (copied from
# hexgate_mcp/tools/policy_apply.py): the API may have committed.
NO_ANSWER = (
    "the Hexgate API did not answer; the change may be saved: read the files "
    "again with policy_read_file before retrying"
)
# After either kind of silence. A retry this turn would send the hashes read
# at turn start again, which is a 409 stale whether or not it was saved.
AFTER_SILENCE = (
    "Within this turn a retry would send the same file hashes, so do not "
    "call apply_change again; tell the user to look at the "
    "Policies page and send their message again if the change is not there."
)
# Our own limit ran out before the server answered.
NO_REPLY = "the MCP server did not reply in time; the change may be saved."


class ChangeFile(BaseModel):
    """A whole file, added or replaced."""

    # A misspelt key is refused to the model, not dropped.
    model_config = ConfigDict(extra="forbid")

    name: str
    content: str


class ChangeSample(BaseModel):
    """A tool call and the outcome it must get once the change is saved."""

    model_config = ConfigDict(extra="forbid")  # as ChangeFile

    agent: str = "*"
    role: str
    tool: str
    args: dict[str, Any] = {}
    attributes: dict[str, Any] = {}
    expect: Literal["allow", "deny", "approval_required"]


def build_apply_tool(mcp_url: str) -> Tool[TurnDeps]:
    async def apply_change(
        ctx: RunContext[TurnDeps],
        files: list[ChangeFile],
        summary: str,
        samples: list[ChangeSample] | None = None,
    ) -> str:
        """Save a change to the project's policy files, once every platform
        check passes: 1 to 10 whole files, each added or replaced, never
        deleted. `summary` is one sentence on what the change is for.
        `samples` (up to 20) are tool calls whose outcome proves the change
        does what was asked, each with the outcome it must get.
        `applied: true` means it is saved; `applied: false` means a check
        failed and nothing was saved, `checks` says which."""
        writes = ctx.deps.writes
        if writes is None:
            return NO_SCOPE
        async with writes.lock:
            text = await _attempt(
                mcp_url, ctx.deps, reads_in(ctx.messages), files, summary, samples
            )
            if writes.change_id is not None and writes.applied_call is None:
                writes.applied_call = ctx.tool_call_id
        return cap_tool_result(TOOL_NAME, text)

    return Tool(apply_change, name=TOOL_NAME)


async def _attempt(
    mcp_url: str,
    deps: TurnDeps,
    reads: Mapping[str, str],
    files: list[ChangeFile],
    summary: str,
    samples: list[ChangeSample] | None,
) -> str:
    """One call's answer to the model, the MCP call made only when every
    counter allows it."""
    writes = deps.writes
    assert writes is not None
    refusal = _refusal(writes)
    if refusal is not None:
        return refusal
    writes.attempts += 1
    unread = _unread(files, deps.snapshot, reads)
    if unread:
        return READ_FIRST.format(files=", ".join(unread))
    arguments = {
        "project_id": writes.project_id,
        # An existing file's base: the hash the model read it at, which
        # `_unread` has just found equal to the turn's. Null for a new one.
        "files": [
            {**f.model_dump(), "base_hash": deps.snapshot.get(f.name)} for f in files
        ],
        "summary": summary,
        "samples": None if samples is None else [s.model_dump() for s in samples],
        "session_id": writes.session_id,
    }
    # Its own MCP session, for its own read timeout; refusals raised as
    # ToolError, not turned into retries.
    mcp: MCPToolset[None] = MCPToolset(
        mcp_url,
        auth=deps.token,
        read_timeout=APPLY_CALL_SECONDS,
        tool_error_behavior="error",
    )
    try:
        text = await call_text(mcp, "policy_apply", arguments)
    except Exception as exc:
        refusal = _refusal_text(exc)
        if refusal is not None:
            return _refused(refusal, writes)
        # A timeout or a connection lost: whether the API committed is
        # unknown.
        logger.warning("policy_apply gave no answer (%s)", type(exc).__name__)
        return _silence(NO_REPLY, writes)
    _record(text, summary, writes)
    return text


def _unread(
    files: list[ChangeFile], snapshot: Mapping[str, str], reads: Mapping[str, str]
) -> list[str]:
    """The existing files the model did not build from their text as the
    turn found it: never read, or read before someone (or an earlier change)
    saved them again. Its whole-file rewrite would silently undo that save."""
    return [
        f.name
        for f in files
        if f.name in snapshot and reads.get(f.name) != snapshot[f.name]
    ]


def read_hash(text: str) -> tuple[str, str] | None:
    """(name, text_hash) of a `policy_read_file` result the model was given
    whole; None for a refusal, a policy module or a result cut short."""
    try:
        record = json.loads(text)
    except ValueError:
        return None
    if not isinstance(record, dict) or "tier" in record:
        return None
    name, text_hash = record.get("name"), record.get("text_hash")
    if isinstance(name, str) and isinstance(text_hash, str):
        return name, text_hash
    return None


def reads_in(messages: list[ModelMessage]) -> dict[str, str]:
    """{file name: text_hash} of the latest whole read of each file in
    `messages`."""
    reads: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, ToolReturnPart) and part.tool_name == READ_TOOL:
                found = read_hash(part.model_response_str())
                if found is not None:
                    reads[found[0]] = found[1]
    return reads


def _refusal(writes: TurnWrites) -> str | None:
    if writes.change_id is not None:
        return ALREADY_APPLIED.format(change_id=writes.change_id)
    if writes.ended is not None:
        return writes.ended
    if writes.attempts >= MAX_ATTEMPTS:
        return ATTEMPT_LIMIT
    return None


# How the MCP server words the API's 409 refusals (hexgate_mcp/api.py).
_CONFLICT_PREFIX = "409 conflict: "
# What the platform's MCP server (the official SDK's FastMCP) puts before
# every error a tool raises.
_SERVER_PREFIX = "Error executing tool policy_apply: "


def _refusal_text(exc: BaseException) -> str | None:
    """The server's refusal in `exc`, without the prefix its SDK adds. The
    client raises it bare, or in an exception group from the MCP session's
    task group."""
    if isinstance(exc, ToolError):
        return str(exc).removeprefix(_SERVER_PREFIX)
    if isinstance(exc, BaseExceptionGroup):
        matched, rest = exc.split(ToolError)
        if matched is not None and rest is None:
            return _refusal_text(matched.exceptions[0])
    return None


def _refused(message: str, writes: TurnWrites) -> str:
    if message == NO_ANSWER:
        return _silence(NO_ANSWER, writes)
    stale = _stale_files(message)
    if stale is not None:
        return _end(writes, STALE.format(files=", ".join(stale) or "unknown"))
    refused = f"refused, nothing saved: {_without_inputs(message)}"
    if message.startswith(_CONFLICT_PREFIX):
        # Any conflict ends writing: this turn's retry would meet it again.
        return _end(writes, f"{refused}. {CONFLICT}")
    return refused


def _without_inputs(message: str) -> str:
    """A refusal `<status> <reason>: <detail>` with every `input` the API's
    validation error echoes taken out of its detail: they hold the files."""
    head, sep, detail = message.partition(": ")
    try:
        parsed = json.loads(detail)
    except ValueError:
        return message
    kept = drop_inputs(parsed)
    return message if kept == parsed else head + sep + json.dumps(kept)


def drop_inputs(value: Any) -> Any:
    """`value` with every `input` key of a validation error removed, at any
    depth: an error on a file echoes its content there."""
    if isinstance(value, dict):
        return {k: drop_inputs(v) for k, v in value.items() if k != "input"}
    if isinstance(value, list):
        return [drop_inputs(v) for v in value]
    return value


def _silence(what: str, writes: TurnWrites) -> str:
    return _end(writes, f"{what.rstrip('.')}. {AFTER_SILENCE}")


def _end(writes: TurnWrites, why: str) -> str:
    """End writing for the rest of the turn, telling the model `why`."""
    writes.ended = why
    return why


def _stale_files(message: str) -> list[str] | None:
    if not message.startswith(_CONFLICT_PREFIX):
        return None
    try:
        detail = json.loads(message.removeprefix(_CONFLICT_PREFIX))
    except ValueError:
        return None
    if not isinstance(detail, dict) or detail.get("code") != "stale":
        return None
    return [str(f) for f in detail.get("files", [])]


def _record(text: str, summary: str, writes: TurnWrites) -> None:
    """On an applied change, keep its id and send its `change` event."""
    try:
        response = json.loads(text)
    except ValueError:
        return
    if not response.get("applied"):
        return
    writes.change_id = response["change_id"]
    writes.on_change(
        {
            "change_id": response["change_id"],
            "summary": summary,
            "files": response["files"],
            "checks": [
                {"id": c["id"], "status": c["status"], "detail": c.get("detail")}
                for c in response["checks"]
            ],
        }
    )


def call_summary(args: Mapping[str, Any]) -> dict[str, Any]:
    """An `apply_change` call's arguments without file contents: the file
    names and the summary. `args` is what the model sent, so any shape."""
    files = args.get("files")
    names = [
        f["name"]
        for f in (files if isinstance(files, list) else [])
        if isinstance(f, dict) and isinstance(f.get("name"), str)
    ]
    summary = args.get("summary")
    return {"files": names, "summary": summary if isinstance(summary, str) else ""}


def redact_calls(
    messages: list[ModelMessage], applied: Mapping[str, str]
) -> list[ModelMessage]:
    """`messages` with every `apply_change` call's arguments replaced by
    `{change_id, files, summary}`: the id of the change it applied
    (`applied[call id]`) or None, and its :func:`call_summary`. A call
    already replaced, from an earlier request or turn, is kept as it is."""
    return [
        _redact(m, applied) if isinstance(m, ModelResponse) else _redact_retries(m)
        for m in messages
    ]


def _redact_retries(request: ModelRequest) -> ModelRequest:
    """A request with each `apply_change` retry's validation errors
    stripped of the arguments they echo."""
    parts = [redact_retry(p) for p in request.parts]
    return request if parts == list(request.parts) else replace(request, parts=parts)


def redact_retry[P](part: P) -> P:
    """`part`, or when it answers an `apply_change` call with validation
    errors (a retry), the same part without each error's `input`: it holds
    the files."""
    if (
        isinstance(part, ToolReturnPart | RetryPromptPart)
        and part.tool_name == TOOL_NAME
        and isinstance(part.content, list)
    ):
        return replace(part, content=drop_inputs(part.content))
    return part


def _redact(response: ModelResponse, applied: Mapping[str, str]) -> ModelResponse:
    parts = [
        replace(
            p,
            args={
                "change_id": applied.get(p.tool_call_id),
                **call_summary(p.args_as_dict()),
            },
        )
        if _carries_files(p)
        else p
        for p in response.parts
    ]
    return response if parts == response.parts else replace(response, parts=parts)


def _carries_files(part: object) -> bool:
    return (
        isinstance(part, ToolCallPart)
        and part.tool_name == TOOL_NAME
        and "change_id" not in part.args_as_dict()
    )


def applied_calls(writes: TurnWrites | None) -> dict[str, str]:
    """{call id: change id} for the call that applied this turn's change."""
    if writes is None or writes.applied_call is None or writes.change_id is None:
        return {}
    return {writes.applied_call: writes.change_id}


async def redact_history(
    ctx: RunContext[TurnDeps], messages: list[ModelMessage]
) -> list[ModelMessage]:
    """Before every model request: a call's file contents are sent to the
    model once, in the request that made it."""
    return redact_calls(messages, applied_calls(ctx.deps.writes))
