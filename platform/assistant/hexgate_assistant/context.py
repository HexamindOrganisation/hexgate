"""What the model is given on a turn beyond its history: the project as it
stands at turn start, the session's latest change and the editor, all built
by our code; and the two hard caps on what reaches it, a tool result's size
and the conversation's.
"""

import asyncio
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolReturnPart,
)

# Tokens are estimated, never counted: no tokenizer runs here. Three bytes a
# token is fewer than English text and most JSON average, so the estimate
# usually comes out high. It can run low on dense text (hex hashes,
# digit-heavy JSON); the next response's reported usage then corrects it, so
# a cap set in tokens is soft by at most one step's tool results.
BYTES_PER_TOKEN = 3

# A tool result reaches the model at most this long, its start and end kept.
TOOL_RESULT_TOKENS = 8000
# Of the context budget, the share kept free for the answer.
ANSWER_SHARE = 0.2
# Names listed on the project card, per kind.
CARD_NAMES = 50

CONTEXT_FULL_MESSAGE = "This conversation is full; start a new chat."


class ContextFull(Exception):
    """The conversation no longer fits the model's context budget."""


class ProjectUnreadable(Exception):
    """The turn-start reads failed: the MCP server down, or refusing them."""


def estimate_tokens(text: str) -> int:
    return -(-len(text.encode()) // BYTES_PER_TOKEN)


def definition_tokens(tools: Iterable[tuple[str, str | None, dict[str, Any]]]) -> int:
    """The estimated size of tool definitions, each (name, description,
    input schema), as a model is offered them."""
    offered = [
        {"name": name, "description": description, "parameters": schema}
        for name, description, schema in tools
    ]
    return estimate_tokens(json.dumps(offered))


def keep_ends(text: str, max_bytes: int, marker: str) -> str:
    """`text`, or when it is over `max_bytes` (UTF-8) its start and end with
    `marker` between them, `{cut}` in it replaced by the bytes left out. The
    result is at most `max_bytes` long."""
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text
    note = marker.format(cut=len(encoded))
    room = max(max_bytes - len(note.encode()), 0)
    head = encoded[: room - room // 2].decode(errors="ignore")
    tail = encoded[len(encoded) - room // 2 :].decode(errors="ignore")
    note = marker.format(cut=len(encoded) - len(head.encode()) - len(tail.encode()))
    return f"{head}{note}{tail}"


# Tools whose result is kept whole up to WHOLE_RESULT_BYTES: a reference
# section or a policy file cut in the middle would be rewritten from what is
# left, and neither tool has a narrower call. Over it, a policy file is
# refused and a reference section is cut like any other result.
WHOLE_RESULT_TOOLS = frozenset({"policy_reference", "policy_read_file"})
WHOLE_RESULT_BYTES = 64 * 1024


def cap_tool_result(tool: str, text: str) -> str:
    """A tool result as the model receives it."""
    size = _whole_result_size(tool, text)
    if tool in WHOLE_RESULT_TOOLS and size <= WHOLE_RESULT_BYTES:
        return text
    if tool == "policy_read_file":
        return (
            f"file too large for the assistant (over "
            f"{WHOLE_RESULT_BYTES // 1024} KiB: {size // 1024} KiB); "
            "edit it in the editor"
        )
    return keep_ends(
        text,
        TOOL_RESULT_TOKENS * BYTES_PER_TOKEN,
        "\n[… {cut} bytes cut from the middle of this result: call the tool "
        "again with a narrower request to read them …]\n",
    )


def _whole_result_size(tool: str, text: str) -> int:
    """The bytes the 64 KiB rule is about: a policy file's own text, not the
    JSON record that carries it (its hashes, its date, every newline and
    quote escaped), which runs larger; any other result as it stands."""
    if tool == "policy_read_file":
        try:
            content = json.loads(text)["content"]
        except (ValueError, KeyError, TypeError):
            # Not a whole record (the server cut it): measured as it stands.
            pass
        else:
            if isinstance(content, str):
                return len(content.encode())
    return len(text.encode())


# ——— The context budget ———


def context_budget(model_window: int | None, configured: int) -> int:
    """Tokens the conversation may fill: the smaller of the model's input
    window and the configured cap, less the share kept for the answer."""
    window = configured if model_window is None else min(model_window, configured)
    return int(window * (1 - ANSWER_SHARE))


def context_tokens(messages: Sequence[ModelMessage], prefix_tokens: int) -> int:
    """The conversation's size: the last response's reported input and
    output tokens, which count the prefix the provider was sent, plus an
    estimate of what came after it. With no reported usage yet, an estimate
    of every message plus `prefix_tokens`."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, ModelResponse) and message.usage.input_tokens:
            reported = message.usage.input_tokens + message.usage.output_tokens
            return reported + _estimate_messages(messages[index + 1 :])
    return prefix_tokens + _estimate_messages(messages)


def _estimate_messages(messages: Sequence[ModelMessage]) -> int:
    if not messages:
        return 0
    dumped = ModelMessagesTypeAdapter.dump_json(without_instructions(messages))
    return estimate_tokens(dumped.decode())


def without_instructions(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    """`messages` without the instructions each request carries: the same
    text on every request, sent once per request as the prefix and counted
    there, never read back from the history."""
    return [
        replace(m, instructions=None) if isinstance(m, ModelRequest) else m
        for m in messages
    ]


@dataclass(frozen=True)
class BudgetGuard:
    """Run before every model request: ends the turn with
    :class:`ContextFull` once the conversation is over `budget`. Nothing is
    summarised."""

    budget: int
    prefix_tokens: int

    def __call__(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        if context_tokens(messages, self.prefix_tokens) > self.budget:
            raise ContextFull
        return messages


# ——— The project at turn start ———


@dataclass(frozen=True)
class ProjectState:
    """The project as the turn found it, read once through MCP."""

    # {file name: text_hash}: the base each write of this turn is checked
    # against. Taken once; a write never re-reads it.
    snapshot: Mapping[str, str]
    # "compose" (a policy.yaml), "modules" (the older module store) or
    # "empty".
    layout: str
    # None when the turn-start read failed: unknown, not empty.
    roles: tuple[str, ...] | None
    agents: tuple[str, ...] | None


async def read_project(mcp: MCPToolset[Any], project_id: str) -> ProjectState:
    """The turn-start reads, as the user. The roles and agents are best
    effort: a project with no policy store has none."""
    args = {"project_id": project_id}
    listed, (roles, agents) = await asyncio.gather(
        call_text(mcp, "policy_list_files", args), _read_graph(mcp, args)
    )
    listed = _file_list(listed)
    snapshot = {f["name"]: f["text_hash"] for f in listed["files"]}
    if "policy.yaml" in snapshot:
        layout = "compose"
    elif listed["modules"]:
        layout = "modules"
    else:
        layout = "empty"
    return ProjectState(snapshot=snapshot, layout=layout, roles=roles, agents=agents)


# How the MCP server ends a result it cut short at its size cap (its
# tools/__init__.py; copied, since this package never imports hexgate_mcp).
# A file list cut there is no longer JSON.
TRUNCATED_MARKER = '\n{"truncated": true}'


def _file_list(text: str) -> dict[str, Any]:
    """The parsed `policy_list_files` result. Raises :class:`ProjectUnreadable`
    when the server cut it short: a partial list would leave files out of the
    snapshot every write is checked against."""
    try:
        return json.loads(text)
    except ValueError as exc:
        if text.endswith(TRUNCATED_MARKER):
            raise ProjectUnreadable(
                "the project has more policy files and modules than the "
                "platform lists in one answer"
            ) from exc
        raise ProjectUnreadable("the platform's policy file list is not JSON") from exc


async def _read_graph(
    mcp: MCPToolset[Any], args: dict[str, Any]
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None]:
    try:
        graph = json.loads(await call_text(mcp, "policy_graph", args))
    except (ModelRetry, ValueError):
        # No policy store, a policy that does not compose, or a graph cut
        # short (no longer JSON): the card says they are unknown, and the
        # model can read them itself.
        return None, None
    return tuple(graph["roles"]), tuple(graph["declared_agents"])


async def call_text(mcp: MCPToolset[Any], tool: str, args: dict[str, Any]) -> str:
    """A direct MCP tool call's result, as text."""
    return as_text(await mcp.direct_call_tool(tool, args))


def as_text(result: Any) -> str:
    """A tool result as the model reads it, whatever its type."""
    return ToolReturnPart("", result).model_response_str()


# The reference sections the model is given beside its instructions, after
# the list of every section.
REFERENCE_SECTIONS = ("yaml-shape", "writing-conditions")


async def read_reference(mcp: MCPToolset[Any]) -> str:
    """The policy reference the model is given beside its instructions: the
    section index, then each of :data:`REFERENCE_SECTIONS`."""
    calls = [{}, *({"section": s} for s in REFERENCE_SECTIONS)]
    texts = await asyncio.gather(
        *(call_text(mcp, "policy_reference", a) for a in calls)
    )
    return "\n\n".join(["# Policy reference", *texts])


# ——— The user message ———


@dataclass(frozen=True)
class Editor:
    open_file: str | None
    unsaved: tuple[str, ...]


# The card's name lists share its budget (CARD_TOKENS) equally, each list
# kept to whole names.
NAMES_LINE_BYTES = 900


def project_card(project: ProjectState, *, may_write: bool, today: date) -> str:
    """The project as the turn found it. Whether the turn may write stands
    in for the user's role: the token names neither the role nor the
    project's name, and files are listed by name, without reading each."""
    lines = [
        f"Today: {today.isoformat()}",
        f"This turn may write: {'yes' if may_write else 'no'}",
        f"Layout: {project.layout}",
        _names("Files", sorted(project.snapshot)),
        _names("Roles", project.roles),
        _names("Agents", project.agents),
    ]
    return "\n".join(lines)


def _names(label: str, names: Sequence[str] | None) -> str:
    """`label: a, b, c (and N more)`: up to CARD_NAMES whole names, as many
    as fit in NAMES_LINE_BYTES."""
    if names is None:
        return f"{label}: unknown (not readable at turn start)"
    if not names:
        return f"{label}: none"
    # Room kept for the longest possible "(and N more)".
    room = NAMES_LINE_BYTES - len(f"{label}:  (and {len(names)} more)".encode())
    shown: list[str] = []
    for name in names[:CARD_NAMES]:
        size = len(name.encode()) + (2 if shown else 0)
        if size > room:
            break
        shown.append(name)
        room -= size
    more = len(names) - len(shown)
    return f"{label}: {', '.join(shown)}" + (f" (and {more} more)" if more else "")


def latest_change_text(change: Mapping[str, Any] | None) -> str:
    """The session's latest applied change, from its stored `change` event."""
    if change is None:
        return "none"
    files = ", ".join(
        f"{f['name']}: {f['before_hash'] or 'new file'}" for f in change["files"]
    )
    checks = ", ".join(f"{c['id']} {c['status']}" for c in change["checks"])
    return "\n".join(
        [
            f"Change: {change['change_id']}",
            f"Files (name: hash before): {files}",
            f"Checks: {checks}",
        ]
    )


def editor_text(editor: Editor) -> str:
    unsaved = ", ".join(editor.unsaved) or "none"
    return f"Open file: {editor.open_file or 'none'}\nUnsaved edits in: {unsaved}"


# Each part of the user message is at most this long, so a long name list
# cannot crowd out the message. The card keeps to it by construction (three
# NAMES_LINE_BYTES lines and a short header); the cut below never reaches it.
CARD_TOKENS = 1000
CHANGE_TOKENS = 500
EDITOR_TOKENS = 1000


def user_message(*, card: str, latest_change: str, editor: str, message: str) -> str:
    """The turn's user message. The names in the first three parts come from
    policy files and the editor: data, never instructions."""
    parts = [
        ("project", card, CARD_TOKENS),
        ("latest_change", latest_change, CHANGE_TOKENS),
        ("editor", editor, EDITOR_TOKENS),
    ]
    blocks = [
        f"<{tag}>\n{keep_ends(text, tokens * BYTES_PER_TOKEN, ' … ')}\n</{tag}>"
        for tag, text, tokens in parts
    ]
    return "\n\n".join([*blocks, f"<message>\n{message}\n</message>"])
