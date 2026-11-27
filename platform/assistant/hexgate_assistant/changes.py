"""`apply_change`: the model's one write tool.

The model sends whole files, a summary and samples. Our code checks each
existing file against the model's latest read of it, then calls the MCP
server's `policy_apply` with that read's hash as the file's base hash; the
platform runs every check and saves only if all of them pass. The model is never offered `policy_apply` itself, so
the limits below cannot be stepped around.

Per turn: at most `MAX_ATTEMPTS` calls and one applied change. A file read
before someone saved it again is refused without a call, so a whole-file
rewrite never undoes that save; a file that changed after this turn began,
a 409 or an answer that never came ends writing for the turn.
"""

import json
import logging
import re
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from typing import Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict
from pydantic_ai import RunContext, Tool
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    INVALID_JSON_KEY,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)

from hexgate_assistant.context import TOO_LARGE, call_text, cap_tool_result
from hexgate_assistant.deps import Applied, TurnDeps, TurnWrites

logger = logging.getLogger(__name__)

TOOL_NAME = "apply_change"
READ_TOOL = "policy_read_file"
MAX_ATTEMPTS = 3

# The MCP tool's own limit on its call to the API (copied from
# hexgate_mcp/tools/policy_apply.py, since this package never imports it).
APPLY_TIMEOUT_SECONDS = 30.0
# Ours on the MCP call, above the server's: the server's limit must end
# first, so a slow apply comes back as its "did not answer; the change may
# be saved" while the API may still commit. The margin covers the server's
# connect phase, which httpx times apart from the read.
APPLY_CALL_SECONDS = APPLY_TIMEOUT_SECONDS + 15
# An apply starts only with this long left in the turn: a turn's deadline
# cutting the call would leave a change the API commits with no record.
APPLY_START_SECONDS = APPLY_CALL_SECONDS + 5

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
NO_TIME = (
    "refused, nothing saved: not enough time left in this turn to apply a "
    "change. Tell the user to send their message again."
)
READ_FIRST = (
    "refused, nothing saved: {files}: changed since you last read it, or "
    "not read before this call. Read it again with policy_read_file, then "
    "rebuild the change from what it returns."
)
TOO_LARGE_TO_EDIT = (
    "refused, nothing saved: {files}: too large for the assistant to edit. "
    "Tell the user to make this change in the editor."
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
# After any silence: a file read again in this turn would differ from the
# turn's snapshot, so any re-read and retry belong in the next message.
AFTER_SILENCE = (
    "Do not read the files again or call apply_change again in this turn. "
    "Tell the user to look at the Policies page first, and to send their "
    "message again only if the change is not there."
)
NOT_SENT = (
    "refused, nothing saved: the MCP server could not be reached before the "
    "change was sent. Try again."
)
# Our own limit ran out before the server answered.
NO_REPLY = "the MCP server did not reply in time; the change may be saved"
# An answer that says applied but cannot be read as a change.
UNREADABLE = "the platform's answer could not be read; the change may be saved"
# A long `applied: false` answer, cut short: no narrower call exists.
CUT_CHECKS = "\n[… {cut} bytes of check details cut …]\n"


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
        deleted. Read each existing file with policy_read_file in this
        conversation first and build it from that text; on "read it again",
        read and retry. `summary` is one sentence on what the change is for.
        `samples` (up to 20) are tool calls whose outcome proves the change
        does what was asked, each with the outcome it must get.
        `applied: true` means it is saved; `applied: false` means a check
        failed and nothing was saved, `checks` says which."""
        writes = ctx.deps.writes
        if writes is None:
            return NO_SCOPE
        call = _Call(mcp_url, ctx, writes, files, summary, samples)
        async with writes.lock:
            text = await call.attempt()
        return cap_tool_result(TOOL_NAME, text, CUT_CHECKS)

    return Tool(apply_change, name=TOOL_NAME)


@dataclass
class _Call:
    """One `apply_change` call: its counters checked, then the MCP call."""

    mcp_url: str
    ctx: RunContext[TurnDeps]
    writes: TurnWrites
    files: list[ChangeFile]
    summary: str
    samples: list[ChangeSample] | None

    async def attempt(self) -> str:
        """The answer to the model, the MCP call made only when every
        counter allows it."""
        refusal = _refusal(self.writes)
        if refusal is not None:
            return refusal
        self.writes.attempts += 1
        left = self.ctx.remaining_time()
        if left is not None and left < APPLY_START_SECONDS:
            return NO_TIME
        refusal = self._read_refusal()
        if refusal is not None:
            return refusal
        return await self._apply()

    def _read_refusal(self) -> str | None:
        """Refused without a call: an existing file the model did not build
        from its text as the turn found it."""
        snapshot = self.ctx.deps.snapshot
        reads = reads_in(self.ctx.messages, self.writes.turn_start)
        existing = [f.name for f in self.files if f.name in snapshot]
        # Refused as too large in this turn; an older refusal may be stale.
        too_large = [
            n
            for n in existing
            if n in reads and reads[n].this_turn and reads[n].text_hash is None
        ]
        if too_large:
            return TOO_LARGE_TO_EDIT.format(files=", ".join(too_large))
        differs = [
            n for n in existing if n not in reads or reads[n].text_hash != snapshot[n]
        ]
        # Read in this turn and still not the turn's text: saved since.
        moved = [n for n in differs if n in reads and reads[n].this_turn]
        if moved:
            return _end(self.writes, STALE.format(files=", ".join(moved)))
        if differs:
            return READ_FIRST.format(files=", ".join(differs))
        return None

    async def _apply(self) -> str:
        writes = self.writes
        snapshot = self.ctx.deps.snapshot
        arguments = {
            "project_id": writes.project_id,
            # An existing file's base: the hash the model read it at, which
            # `_read_refusal` has just found equal to the turn's. Null for a
            # new one.
            "files": [
                {**f.model_dump(), "base_hash": snapshot.get(f.name)}
                for f in self.files
            ],
            "summary": self.summary,
            "samples": None
            if self.samples is None
            else [s.model_dump() for s in self.samples],
            "session_id": writes.session_id,
        }
        # Its own MCP session, for its own read timeout; refusals raised as
        # ToolError, not turned into retries.
        mcp: MCPToolset[None] = MCPToolset(
            self.mcp_url,
            auth=self.ctx.deps.token,
            read_timeout=APPLY_CALL_SECONDS,
            tool_error_behavior="error",
        )
        answer: str | None = None
        try:
            async with AsyncExitStack() as session:
                try:
                    await session.enter_async_context(mcp)
                    # Listed first: the client checks a call's result against
                    # the tool's listed schema, and would otherwise list the
                    # tools after the call, a failure there losing an answer
                    # the API has committed.
                    await mcp.list_tools()
                except Exception as exc:
                    return _not_sent(exc)
                answer = await self._call(mcp, arguments)
        except Exception as exc:
            # Closing the session failed: the answer, if any, stands.
            if answer is None:
                return _not_sent(exc)
        assert answer is not None
        return answer

    async def _call(self, mcp: MCPToolset[None], arguments: dict[str, Any]) -> str:
        """The answer to the `policy_apply` call itself."""
        try:
            text = await call_text(mcp, "policy_apply", arguments)
        except Exception as exc:
            refusal = _refusal_text(exc)
            if refusal is not None:
                return _refused(refusal, self.writes)
            # A timeout or a connection lost: whether the API committed is
            # unknown.
            logger.warning("policy_apply gave no answer (%s)", type(exc).__name__)
            return _silence(NO_REPLY, self.writes)
        return self._record(text)

    def _record(self, text: str) -> str:
        """The answer to an MCP result. An applied change is kept and its
        `change` event sent; one that cannot be read may be saved."""
        try:
            response = json.loads(text)
        except ValueError:
            return _silence(UNREADABLE, self.writes)
        if not isinstance(response, dict) or not response.get("applied"):
            return text
        try:
            event = {
                "change_id": response["change_id"],
                "summary": self.summary,
                "files": response["files"],
                "checks": [
                    {"id": c["id"], "status": c["status"], "detail": c.get("detail")}
                    for c in response["checks"]
                ],
            }
        except (KeyError, TypeError):
            return _silence(UNREADABLE, self.writes)
        self.writes.applied = Applied(event["change_id"], self.ctx.tool_call_id)
        self.writes.on_change(event)
        return text


@dataclass(frozen=True)
class Read:
    """The model's latest read of a file."""

    # None: refused as too large, so never read whole.
    text_hash: str | None
    this_turn: bool


def reads_in(messages: list[ModelMessage], turn_start: int) -> dict[str, Read]:
    """{file name: its latest read} in `messages`, a read in a message at or
    after `turn_start` made in this turn."""
    names: dict[str, str] = {}
    reads: dict[str, Read] = {}
    for index, message in enumerate(messages):
        for part in message.parts:
            if isinstance(part, ToolCallPart) and part.tool_name == READ_TOOL:
                args = part.args_as_dict()
                if isinstance(args.get("name"), str) and args.get("tier") is None:
                    names[part.tool_call_id] = args["name"]
            elif isinstance(part, ToolReturnPart) and part.tool_call_id in names:
                text = part.model_response_str()
                text_hash = _text_hash(text)
                if text_hash is not None or text.startswith(TOO_LARGE):
                    name = names[part.tool_call_id]
                    reads[name] = Read(text_hash, index >= turn_start)
    return reads


def _text_hash(text: str) -> str | None:
    """The text_hash of a `policy_read_file` result the model was given
    whole; None for anything else."""
    try:
        record = json.loads(text)
    except ValueError:
        return None
    if isinstance(record, dict) and isinstance(record.get("text_hash"), str):
        return record["text_hash"]
    return None


def _refusal(writes: TurnWrites) -> str | None:
    if writes.applied is not None:
        return ALREADY_APPLIED.format(change_id=writes.applied.change_id)
    if writes.ended is not None:
        return writes.ended
    if writes.attempts >= MAX_ATTEMPTS:
        return ATTEMPT_LIMIT
    return None


# How the MCP server words the API's 409 refusals (hexgate_mcp/api.py).
CONFLICT_PREFIX = "409 conflict: "
# What the platform's MCP server (the official SDK's FastMCP) puts before
# every error a tool raises.
SERVER_PREFIX = "Error executing tool policy_apply: "


def _refusal_text(exc: BaseException) -> str | None:
    """The server's refusal in `exc`, without the prefix its SDK adds. The
    client raises it bare, or in an exception group from the MCP session's
    task group."""
    if isinstance(exc, ToolError):
        return str(exc).removeprefix(SERVER_PREFIX)
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
    if message.startswith(CONFLICT_PREFIX):
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


def _not_sent(exc: Exception) -> str:
    """Before the call was sent: nothing can have been saved, and writing
    goes on."""
    logger.warning("policy_apply not sent (%s)", type(exc).__name__)
    return NOT_SENT


def _silence(what: str, writes: TurnWrites) -> str:
    return _end(writes, f"{what}. {AFTER_SILENCE}")


def _end(writes: TurnWrites, why: str) -> str:
    """End writing for the rest of the turn, telling the model `why`."""
    writes.ended = why
    return why


def _stale_files(message: str) -> list[str] | None:
    if not message.startswith(CONFLICT_PREFIX):
        return None
    try:
        detail = json.loads(message.removeprefix(CONFLICT_PREFIX))
    except ValueError:
        return None
    if not isinstance(detail, dict) or detail.get("code") != "stale":
        return None
    return [str(f) for f in detail.get("files", [])]


# ——— File contents reach the model once ———

# Where a tool's arguments carry whole files: `apply_change`'s files, the
# draft checks' drafts.
FILE_KEYS = ("files", "drafts")
# A file name, which the platform does not restrict: any one line. File
# text runs over several lines and fails it.
_FILE_NAME = re.compile(r"[^\x00-\x1f]{1,255}")


def redact(
    tool: str, args: Mapping[str, Any], change_id: str | None = None
) -> Mapping[str, Any]:
    """A tool call's arguments as they are kept and shown once the call is
    made, without the file text they may carry.

    An `apply_change` call always becomes `{change_id, files, summary}`,
    whatever shape the model gave it: `files` the names it can tell apart,
    else none, and `change_id` ours, the one its applied result gave, never
    one in `args`. Any other call loses the text under its file keys (each
    item reduced to its name, or the key dropped when it does not reduce
    cleanly), and, when its arguments were cut short and mention content,
    all of them. Otherwise it is returned as it is."""
    if tool == TOOL_NAME:
        summary = args.get("summary")
        return {
            "change_id": change_id,
            "files": _names(args.get("files")) or [],
            "summary": summary if isinstance(summary, str) else "",
        }
    raw = args.get(INVALID_JSON_KEY)
    if isinstance(raw, str):
        return {} if "content" in raw else args
    if not any(k in FILE_KEYS or _has_content(v) for k, v in args.items()):
        return args
    kept: dict[str, Any] = {}
    for key, value in args.items():
        if key in FILE_KEYS or _has_content(value):
            names = _names(value)
            if names is not None:
                kept[key] = names
        else:
            kept[key] = value
    return kept


def _has_content(value: Any) -> bool:
    return isinstance(value, list) and any(
        isinstance(item, dict) and "content" in item for item in value
    )


def _names(value: Any) -> list[str] | None:
    """The file names of a file list (`[{name, content}]`, or the names
    themselves); None when it is anything else, text included."""
    if not isinstance(value, list):
        return None
    names = []
    for item in value:
        name = item.get("name") if isinstance(item, dict) else item
        if not isinstance(name, str) or not _FILE_NAME.fullmatch(name):
            return None
        names.append(name)
    return names


def redact_calls(
    messages: list[ModelMessage], applied: Applied | None
) -> list[ModelMessage]:
    """`messages` with every tool call's arguments :func:`redact`ed, and
    every retry's validation errors without the arguments they echo."""
    return [
        _redact_response(m, applied)
        if isinstance(m, ModelResponse)
        else _redact_retries(m)
        for m in messages
    ]


def _redact_response(response: ModelResponse, applied: Applied | None) -> ModelResponse:
    parts = [
        _redact_call(p, applied) if isinstance(p, ToolCallPart) else p
        for p in response.parts
    ]
    return response if parts == response.parts else replace(response, parts=parts)


def _redact_call(part: ToolCallPart, applied: Applied | None) -> ToolCallPart:
    args = part.args_as_dict()
    mine = applied is not None and applied.call_id == part.tool_call_id
    kept = redact(part.tool_name, args, applied.change_id if mine else None)
    return part if kept is args else replace(part, args=dict(kept))


def _redact_retries(request: ModelRequest) -> ModelRequest:
    parts = [redact_retry(p) for p in request.parts]
    return request if parts == list(request.parts) else replace(request, parts=parts)


# How pydantic quotes the value it refused in a validation error's text.
_INPUT_VALUE = re.compile(r"input_value=.*?(?=, input_type=|\]|$)", re.DOTALL)
_CUT_INPUT = "input_value=…"
# The outcome pydantic-ai gives a tool return that asks for a retry.
RETRIED = "retried"


def redact_retry[P](part: P) -> P:
    """`part`, or when it answers a call with validation errors (a retry),
    the same part without each error's `input`: it can hold whole files."""
    retry = isinstance(part, RetryPromptPart) or (
        isinstance(part, ToolReturnPart) and part.outcome == RETRIED
    )
    if retry and isinstance(part.content, list):
        return replace(part, content=drop_inputs(part.content))
    if retry and isinstance(part.content, str):
        return replace(part, content=_INPUT_VALUE.sub(_CUT_INPUT, part.content))
    return part


async def redact_history(
    ctx: RunContext[TurnDeps], messages: list[ModelMessage]
) -> list[ModelMessage]:
    """Before every model request: the file text of every call made so far
    leaves the history; the model sent it, and needs it no more."""
    writes = ctx.deps.writes
    return redact_calls(messages, None if writes is None else writes.applied)
