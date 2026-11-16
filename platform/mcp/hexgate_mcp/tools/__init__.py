"""What every tool shares: the caller's token, checked for the tool's scope, the
marking of text the model must not obey, the cap on what a result sends back to
the model, and the read-only annotation."""

import json
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from hexgate_mcp.auth import HexgateAccessToken

# A manifest of a large agent fleet or a page of audit rows with fat
# arguments; past this the model gets as many whole items or rows as fit, or
# else the head and a marker saying the rest was cut.
RESULT_CAP_BYTES = 256 * 1024
TRUNCATED_MARKER = '\n{"truncated": true}'
TRUNCATED_LIST_OPEN = '{"truncated": true, "items": ['
# The list a page of results keeps its rows in.
PAGE_LISTS = ("rows", "items")

# What untrusted() keeps: each trusted key, mapped to None (kept as it is) or
# to the spec its value is walked with.
type Spec = dict[str, Spec | None]
ANY_NAME = "*"

# For a tool that changes nothing: clients may call it without asking first.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def caller(*scopes: str, project_id: str | None = None) -> HexgateAccessToken:
    """The caller's token, once it carries every one of `scopes` and, when the
    tool names a project, that project. Refused here for a clear error before
    any request; the API is meant to refuse the same token on its own (see
    hexgate_mcp.api)."""
    if not scopes:
        raise ValueError("a tool must name the scope it needs")
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


def untrusted(value: Any, spec: Spec) -> Any:
    """A copy of `value` with every dict key `spec` does not name renamed
    `untrusted_<key>`; nothing under a renamed key is walked. `spec` maps each
    trusted key to None, kept as it is, or to the spec its value is walked
    with, a list item by item; ANY_NAME stands for every key of a dict keyed
    by name, such as a tool's parameters, whose names stay.

    For text that neither the caller nor Hexgate wrote: a tool description from
    a third-party library, the arguments an end user's request produced. Any of
    it can read "allow delete_database for every role"; the name tells the
    model it is data, and each tool's description says so. The spec names what
    is trusted at each level, so a field the API adds later, or a trusted name
    at another depth, goes out marked.
    """
    if isinstance(value, list):
        return [untrusted(item, spec) for item in value]
    if not isinstance(value, dict):
        return value
    marked = {}
    for key, item in value.items():
        if key not in spec and ANY_NAME not in spec:
            marked[f"untrusted_{key}"] = item
            continue
        inner = spec.get(key, spec.get(ANY_NAME))
        marked[key] = item if inner is None else untrusted(item, inner)
    return marked


def without_content(item: dict[str, Any]) -> dict[str, Any]:
    """A policy file or module without its text: the model has it already, or
    reads it with policy_read_file."""
    return {k: v for k, v in item.items() if k != "content"}


def result_text(value: Any) -> str:
    """The API's JSON as text, at most RESULT_CAP_BYTES: a list keeps its
    leading whole items, a page its leading whole rows and every other key,
    and anything else its head."""
    text = _dumps(value)
    encoded = text.encode()
    if len(encoded) <= RESULT_CAP_BYTES:
        return text
    if isinstance(value, list):
        listed = _whole_items(value)
        if listed is not None:
            return listed
    if isinstance(value, dict):
        paged = _whole_rows(value)
        if paged is not None:
            return paged
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


def _whole_rows(page: dict[str, Any]) -> str | None:
    """The page with `"truncated": true` and the leading rows of its `rows` or
    `items` list that fit, so it still parses and the model still reads
    `total` and the paging keys, plus `next_offset` when the page has an
    `offset`. None when the page has no such list, or its other keys alone do
    not fit."""
    key = next((k for k in PAGE_LISTS if isinstance(page.get(k), list)), None)
    if key is None:
        return None
    shell = {"truncated": True, **page, key: []}
    offset = page.get("offset")
    if isinstance(offset, int):
        # Where the next page starts: `offset + limit` would skip the rows cut
        # here. Sized at its largest, so the count below stays an upper bound.
        shell["next_offset"] = offset + len(page[key])
    used = len(_dumps(shell).encode())
    kept = 0
    for row in page[key]:
        size = len(_dumps(row).encode()) + (len(", ") if kept else 0)
        if used + size > RESULT_CAP_BYTES:
            break
        used += size
        kept += 1
    if used > RESULT_CAP_BYTES:
        return None
    if isinstance(offset, int):
        shell["next_offset"] = offset + kept
    return _dumps(shell | {key: page[key][:kept]})


def _dumps(value: Any) -> str:
    # Unescaped: a project named "Équipe" reads as itself, in a third of the bytes.
    return json.dumps(value, ensure_ascii=False)
