"""LangChain ``BaseMessage`` objects → OTel GenAI ``role``/``parts`` messages.

The shape translation for the LangChain adapter, kept apart from the callback
handler in ``usage.py`` as ``adapters/openai/messages.py`` is from its hooks:
pure functions over plain dicts that decide nothing about when or whether to
emit. What they share lives in ``adapters/_messages.py``.

Two LangChain differences drive most of this file. Messages name their kind
under ``type``, in their own vocabulary (``human``/``ai``) that has to be
mapped onto GenAI's ``user``/``assistant``; and there is no system-prompt
argument — the system message is just the head of the list, which is what
:func:`split_system_instructions` lifts back out.
"""

from __future__ import annotations

from typing import Any

from hexgate.adapters._messages import as_dict, text_part

# LangChain's ``BaseMessage.type`` vocabulary → the GenAI role names. ``chat``
# is absent on purpose: ``ChatMessage`` carries a caller-chosen ``role``, which
# :func:`_role` reads directly. Anything unmapped falls back to ``user`` — the
# least-privileged reading of a message whose origin we cannot establish.
_ROLES = {
    "system": "system",
    "human": "user",
    "ai": "assistant",
    "tool": "tool",
    # Pre-``ToolMessage`` shape, still emitted by older chains.
    "function": "tool",
}

# Content-block types whose payload is plain text under a ``text`` key.
# Just ``text``: the provider spellings the OpenAI adapter has to handle
# (``output_text`` and friends) are normalised to this one by LangChain's own
# block translators before a callback ever sees them.
_TEXT_BLOCK_TYPES = frozenset({"text"})

# Content-block types that *may* restate a tool call the message also carries
# in its standardized ``tool_calls``: ``function_call`` is the OpenAI Responses
# shape, ``tool_use`` Anthropic's, the rest LangChain's own. Emitting the block
# and the part records one call twice under two ids — the block carries the
# output-item id, the part the call id a later ``ToolMessage`` correlates on.
#
# "May" is the point: being one of these types is not enough to drop a block,
# only being one whose call id the standardized field also carries (see
# :func:`_promoted_call_ids`), since an unpromoted block is the only record of
# that call. ``server_tool_use``/``mcp_tool_use`` are absent deliberately — the
# provider ran those itself, so they never reach ``tool_calls``.
_TOOL_CALL_BLOCK_TYPES = frozenset(
    {"function_call", "tool_use", "tool_call", "invalid_tool_call"}
)


def _role(entry: dict[str, Any]) -> str:
    """The GenAI role for one message dict.

    The chunk classes do not share their parent's ``type``: a streamed
    completion arrives as an ``AIMessageChunk``, whose ``type`` is that literal
    string. Unnormalised it misses the table and falls back to ``user``, filing
    the model's own answer as the user's.
    """
    kind = entry.get("type") or ""
    kind = kind.removesuffix("MessageChunk").lower() or kind
    if kind == "chat":
        return entry.get("role") or "user"
    return _ROLES.get(kind, "user")


def _promoted_call_ids(entry: dict[str, Any]) -> frozenset[str]:
    """The call ids :func:`_tool_call_parts` will emit for this message.

    Read off the standardized fields, so dropping a block is conditional on
    something else actually recording that call. A block's id lives under
    ``call_id`` on the OpenAI Responses shape, ``id`` on the others.

    A missing id normalises to ``""`` on both sides rather than being filtered:
    ``ToolCall.id`` is ``str | None``, and filtering would leave an id-less
    block matching nothing and going out beside the part built from the same
    call — the exact double-record this rule prevents.
    """
    return frozenset(
        call.get("id") or ""
        for calls in (entry.get("tool_calls"), entry.get("invalid_tool_calls"))
        for call in calls or []
    )


def _content_parts(content: Any, promoted: frozenset[str]) -> list[dict[str, Any]]:
    """A message's ``content`` as GenAI parts.

    A text block becomes a ``text`` part; anything else (an image, a
    ``thinking`` block, a provider extension) is carried through whole, since
    dropping it would lose exactly the attachment an investigator came for.
    A tool-call block whose id is in ``promoted`` is dropped, because
    :func:`_tool_call_parts` re-emits that call — see
    :data:`_TOOL_CALL_BLOCK_TYPES`.

    Empty content yields no parts rather than one empty ``text`` part: an
    ``AIMessage`` that only calls a tool has ``content == ""``, and that is the
    common case.
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
    GenAI ``tool_call`` parts.

    ``arguments`` is whatever LangChain put there: a parsed dict for a valid
    call, the raw string the model emitted for an invalid one. Neither is
    re-encoded. Redaction reaches the dict natively and a *parseable* string
    through ``TOOL_CALL_JSON_KEYS`` — but an invalid call's string is by
    construction the one LangChain could not parse, and ``_redact_json_string``
    hands unparseable text back untouched, so a secret in a malformed tool call
    is stored as emitted. Kept anyway, with its parse ``error``: that is
    precisely what a reader is trying to explain, and no key-name rule can open
    a blob that is not JSON.
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


def input_message(message: Any) -> dict[str, Any]:
    """One ``BaseMessage`` (or a bare prompt string) as a GenAI message.

    A ``ToolMessage`` becomes a ``tool_call_response`` rather than text: a
    decision event records that a tool was called but never what it returned,
    so this is the only place that value is stored, and it needs the
    ``tool_call_id`` to be matched back. ``name`` rides along because the
    legacy ``FunctionMessage`` has no ``tool_call_id`` field at all.
    """
    entry = as_dict(message)
    if entry is None:
        # A plain prompt string from the non-chat ``on_llm_start`` path, or a
        # message type we cannot open.
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


def output_messages(response: Any) -> list[dict[str, Any]]:
    """An ``LLMResult`` as the completion of the call that produced it.

    Only ``generations[0]`` belongs to this event — ``generations`` is a list
    per prompt and the callback fires once per call; its entries are the ``n``
    candidates, each its own assistant message. A non-chat LLM yields a bare
    ``Generation`` with no ``message``, whose text is the whole completion.
    """
    generations = response.generations[0] if response.generations else []
    messages: list[dict[str, Any]] = []
    for generation in generations:
        message = getattr(generation, "message", None)
        if message is None:
            messages.append(
                {"role": "assistant", "parts": [text_part(generation.text)]}
            )
        else:
            # Same conversion as the input side: an AIMessage is an AIMessage
            # whether it is being sent or has just come back.
            messages.append(input_message(message))
    return messages


def split_system_instructions(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Lift the leading system messages out of a converted list.

    The instructions are just the head of the list here, while the wire
    contract carries them in their own capped field on the first event of each
    ``turn_key``. Split rather than copied: the system prompt is often the
    largest thing in the list.

    Leading only — one appearing mid-conversation was injected by the chain as
    part of the exchange and stays where it happened.
    """
    cut = 0
    while cut < len(messages) and messages[cut].get("role") == "system":
        cut += 1
    instructions = [part for message in messages[:cut] for part in message["parts"]]
    return instructions, messages[cut:]
