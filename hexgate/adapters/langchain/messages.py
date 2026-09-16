"""LangChain ``BaseMessage`` objects → OTel GenAI ``role``/``parts`` messages.

Pure functions over plain dicts, kept apart from the callback handler in
``usage.py`` as ``adapters/openai/messages.py`` is from its hooks.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult

from hexgate.adapters._messages import as_dict, text_part

# ``chat`` is absent because ``ChatMessage`` carries a caller-chosen ``role``,
# and anything unmapped falls back to ``user``, the least-privileged reading.
_ROLES = {
    "system": "system",
    "human": "user",
    "ai": "assistant",
    "tool": "tool",
    # Pre-``ToolMessage`` shape, still emitted by older chains.
    "function": "tool",
}

# Just ``text``: LangChain's block translators normalise the provider
# spellings (``output_text`` and friends) before a callback ever sees them.
_TEXT_BLOCK_TYPES = frozenset({"text"})

# Block types that *may* restate a tool call the message also carries in its
# standardized ``tool_calls``, which would record one call twice under two ids.
# Type alone never drops a block — see :func:`_promoted_call_ids`.
# ``server_tool_use``/``mcp_tool_use`` are absent because the provider ran
# those itself, so they never reach ``tool_calls``.
_TOOL_CALL_BLOCK_TYPES = frozenset(
    {"function_call", "tool_use", "tool_call", "invalid_tool_call"}
)

# Reasoning blocks, dropped from the *completion* only (issue #221, matching
# ``adapters/openai/messages.py``): every part of the output message shares one
# 8 KiB budget, and ``cap_json_head_tail`` splits it evenly across string
# leaves, so enough reasoning blocks starve the answer beside them — measured
# at 12% of a 2.4 KiB answer surviving 20 of them. Both spellings, because
# LangChain normalises to ``reasoning`` in ``content_blocks`` while the raw
# provider block left in ``content`` is Anthropic's ``thinking``. Kept on the
# input side, where it rides the far larger 256 KiB budget.
_REASONING_BLOCK_TYPES = frozenset({"reasoning", "thinking", "redacted_thinking"})


def _role(entry: dict[str, Any]) -> str:
    """The GenAI role for one message dict.

    The suffix is stripped because a streamed completion arrives as an
    ``AIMessageChunk``, whose ``type`` is that literal string and which would
    otherwise miss the table and file the model's own answer under ``user``.
    """
    kind = entry.get("type") or ""
    kind = kind.removesuffix("MessageChunk").lower() or kind
    if kind == "chat":
        return entry.get("role") or "user"
    return _ROLES.get(kind, "user")


def _promoted_call_ids(entry: dict[str, Any]) -> frozenset[str]:
    """The call ids :func:`_tool_call_parts` will emit, so that dropping a
    block is conditional on something else actually recording that call.

    A missing id normalises to ``""`` rather than being filtered, since
    ``ToolCall.id`` is ``str | None`` and filtering would leave an id-less
    block going out beside the part built from the very same call.
    """
    return frozenset(
        call.get("id") or ""
        for calls in (entry.get("tool_calls"), entry.get("invalid_tool_calls"))
        for call in calls or []
    )


def _content_parts(
    content: str | list[str | dict] | None, promoted: frozenset[str]
) -> list[dict[str, Any]]:
    """A message's ``content`` as GenAI parts.

    A non-text block (an image, a ``thinking`` block) is carried through whole
    rather than dropped, since it is exactly the attachment an investigator
    came for. A tool-call block whose id is in ``promoted`` is dropped because
    :func:`_tool_call_parts` re-emits that call. Empty content yields no parts
    at all, since an ``AIMessage`` that only calls a tool has ``content == ""``.
    """
    if not content:
        return []
    if not isinstance(content, (list, tuple)):
        return [text_part(content)]
    parts: list[dict[str, Any]] = []
    for raw in content:
        block = as_dict(raw)
        if block is None:
            parts.append(text_part(raw))
        elif block.get("type") in _TEXT_BLOCK_TYPES and "text" in block:
            parts.append(text_part(block["text"]))
        elif (
            block.get("type") in _TOOL_CALL_BLOCK_TYPES
            and (block.get("call_id") or block.get("id") or "") in promoted
        ):
            continue
        else:
            parts.append(block)
    return parts


def _tool_call_parts(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``tool_calls`` and ``invalid_tool_calls`` of an ``AIMessage`` as
    GenAI ``tool_call`` parts, neither re-encoded.

    An invalid call's ``arguments`` is by construction the string LangChain
    could not parse, and ``_redact_json_string`` hands unparseable text back
    untouched, so a secret in a malformed tool call is stored as emitted. Kept
    anyway with its ``error``, since no key-name rule can open a blob that is
    not JSON and a broken tool call is what a reader came to explain.
    """
    parts: list[dict[str, Any]] = [
        {
            "type": "tool_call",
            "id": call.get("id") or "",
            "name": call.get("name") or "",
            "arguments": call.get("args"),
        }
        for call in entry.get("tool_calls") or []
    ]
    parts.extend(
        {
            "type": "tool_call",
            "id": call.get("id") or "",
            "name": call.get("name") or "",
            "arguments": call.get("args"),
            "error": call.get("error"),
        }
        for call in entry.get("invalid_tool_calls") or []
    )
    return parts


def input_message(message: BaseMessage | str) -> dict[str, Any]:
    """One ``BaseMessage`` (or a bare prompt string) as a GenAI message.

    A ``ToolMessage`` becomes a ``tool_call_response`` because a decision event
    records that a tool was called but never what it returned, making this the
    only place that value is stored. ``name`` rides along because the legacy
    ``FunctionMessage`` has no ``tool_call_id`` field at all.
    """
    entry = as_dict(message)
    if entry is None:
        # A prompt string from the non-chat path, or a type we cannot open.
        return {"role": "user", "parts": [text_part(message)]}
    role = _role(entry)
    if role == "tool":
        return {
            "role": "tool",
            "parts": [
                {
                    "type": "tool_call_response",
                    "id": entry.get("tool_call_id") or "",
                    "name": entry.get("name") or "",
                    "response": entry.get("content"),
                }
            ],
        }
    return {
        "role": role,
        "parts": _content_parts(entry.get("content"), _promoted_call_ids(entry))
        + _tool_call_parts(entry),
    }


def output_messages(response: LLMResult) -> list[dict[str, Any]]:
    """An ``LLMResult`` as the completion of the call that produced it, minus
    its reasoning (:data:`_REASONING_BLOCK_TYPES`).

    Only ``generations[0]`` belongs to this event, since ``generations`` is a
    list per prompt and the callback fires once per call. A non-chat LLM yields
    a bare ``Generation`` with no ``message``, whose text is the completion.
    """
    generations = response.generations[0] if response.generations else []
    messages: list[dict[str, Any]] = []
    for generation in generations:
        message = getattr(generation, "message", None)
        if message is None:
            messages.append(
                {"role": "assistant", "parts": [text_part(generation.text)]}
            )
            continue
        # An AIMessage converts the same whether sent or just returned; only
        # the reasoning drop is specific to this side.
        converted = input_message(message)
        parts = [
            part
            for part in converted["parts"]
            if part.get("type") not in _REASONING_BLOCK_TYPES
        ]
        # No parts, no message — a completion that was reasoning and nothing
        # else has nothing left to record, as in the OpenAI adapter.
        if parts:
            messages.append({**converted, "parts": parts})
    return messages


def split_system_instructions(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Lift the leading system messages out, since LangChain keeps them at the
    head of the list while the wire contract carries them in their own field.

    Split rather than copied, because the system prompt is often the largest
    thing in the list. Leading only — one appearing mid-conversation was
    injected by the chain as part of the exchange and stays where it happened.
    """
    cut = 0
    while cut < len(messages) and messages[cut].get("role") == "system":
        cut += 1
    instructions = [part for message in messages[:cut] for part in message["parts"]]
    return instructions, messages[cut:]
