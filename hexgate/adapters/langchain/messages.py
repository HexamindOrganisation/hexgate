"""LangChain ``BaseMessage`` objects → OTel GenAI ``role``/``parts`` messages.

The shape translation for the LangChain adapter, kept apart from the callback
handler in ``usage.py`` exactly as ``adapters/openai/messages.py`` is from its
hooks: pure functions over plain dicts that decide nothing about when or
whether to emit. What this layer shares with the other adapters' lives in
``adapters/_messages.py``.

LangChain differs from the Responses API in two ways that matter here. Its
messages name their kind under ``type`` rather than ``role``, with its own
vocabulary (``human``/``ai``) that has to be mapped onto GenAI's
``user``/``assistant``; and there is no separate system-prompt argument — the
system message is simply the head of the list, which is why
:func:`split_system_instructions` exists to lift it back out.
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
_TEXT_BLOCK_TYPES = frozenset({"text"})


def _role(entry: dict[str, Any]) -> str:
    """The GenAI role for one message dict.

    The chunk classes do not share their parent's ``type``: a streamed
    completion arrives as an ``AIMessageChunk``, whose ``type`` is the literal
    ``"AIMessageChunk"``. Unnormalised it misses the table and falls back to
    ``user``, filing the model's own answer as the user's — so the suffix is
    stripped before the lookup rather than each chunk class being listed.
    """
    kind = entry.get("type") or ""
    kind = kind.removesuffix("MessageChunk").lower() or kind
    if kind == "chat":
        return entry.get("role") or "user"
    return _ROLES.get(kind, "user")


def _content_parts(content: Any) -> list[dict[str, Any]]:
    """A message's ``content`` as GenAI parts.

    ``content`` is either a bare string or a list of blocks, each a string or a
    dict naming itself under ``type``. A text block becomes a ``text`` part;
    anything else (an image, an Anthropic ``thinking`` block, a provider
    extension) is carried through whole, since dropping it would lose exactly
    the attachment an investigator came for.

    Empty content yields no parts at all rather than one empty ``text`` part:
    an ``AIMessage`` that only calls a tool has ``content == ""``, and that is
    the common case, not an anomaly.
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
        else:
            parts.append(block)
    return parts


def _tool_call_parts(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``tool_calls`` and ``invalid_tool_calls`` of an ``AIMessage`` as
    GenAI ``tool_call`` parts.

    ``arguments`` is whatever LangChain put there: a parsed dict for a valid
    call, the raw string the model emitted for an invalid one. Neither is
    re-encoded. Redaction reaches the dict natively, and reaches a *parseable*
    string through ``TOOL_CALL_JSON_KEYS`` — but an invalid call's string is by
    construction the one LangChain could not parse, and ``_redact_json_string``
    hands unparseable text back untouched. So a secret inside a malformed tool
    call is stored as the model emitted it. Kept anyway, with its parse
    ``error``: a model that asked for a tool in broken JSON is precisely what a
    reader is trying to explain, and no key-name rule can open a blob that is
    not JSON.
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

    A ``ToolMessage`` becomes a ``tool_call_response`` part rather than a text
    one: a decision event records that a tool was called but never what it
    returned, so this is the only place that value is stored, and it needs the
    ``tool_call_id`` beside it to be matched back to the call.
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
                    "response": entry.get("content"),
                }
            ],
        }
    return {
        "role": role,
        "parts": _content_parts(entry.get("content")) + _tool_call_parts(entry),
    }


def output_messages(response: Any) -> list[dict[str, Any]]:
    """An ``LLMResult`` as the completion of the call that produced it.

    ``generations`` is a list per prompt, and the callback fires once per LLM
    call, so only ``generations[0]`` belongs to this event; its own entries are
    the ``n`` candidates the provider returned — normally one, and each is its
    own assistant message.

    A non-chat LLM yields a bare ``Generation`` with no ``message``, in which
    case the generated text is the whole completion.
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

    LangChain has no separate system-prompt argument — the instructions are
    just the head of the message list — while the wire contract carries them in
    their own capped field, on the first event of each ``turn_key``. Splitting
    rather than copying: the system prompt is often the largest thing in the
    list, and storing it twice on the same event buys nothing.

    Leading only. A system message that appears mid-conversation was injected
    by the chain as part of the exchange and stays in the transcript where it
    happened.
    """
    cut = 0
    while cut < len(messages) and messages[cut].get("role") == "system":
        cut += 1
    instructions = [part for message in messages[:cut] for part in message["parts"]]
    return instructions, messages[cut:]
