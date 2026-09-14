"""Responses-API items → OTel GenAI ``role``/``parts`` messages.

The shape translation for the OpenAI Agents adapter, kept apart from the
``RunHooks`` in ``usage.py``: these are pure functions over plain dicts that
decide nothing about when or whether to emit. Each of the other three
adapters needs the same layer against its own message types, so each gets
its own ``messages.py`` beside its hooks.

The target shape is the official GenAI one — a message is ``{"role": …,
"parts": [...]}`` and a part names itself under ``type`` — which is what
``hexgate.tracing.messages.LlmMessageEvent`` puts on the wire and what the
``llm_message`` content columns store. Nothing here is lossy on purpose: an
item with no GenAI equivalent is carried through whole rather than dropped,
since the transcript exists to explain a run after the fact.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Content-part types whose payload is plain text under a ``text`` key. The
# Responses API names the same thing differently by direction (``input_text``
# on the way in, ``output_text`` on the way back), reasoning summaries add
# ``summary_text``, and a bare ``text`` turns up in hand-built items and in
# a message replayed from a stored transcript. GenAI has one ``text`` part,
# so they all collapse into it.
_TEXT_PART_TYPES = frozenset({"input_text", "output_text", "text", "summary_text"})


def _as_dict(item: Any) -> dict[str, Any] | None:
    """One Responses-API item as a plain dict, or ``None`` if it is neither a
    mapping nor a pydantic model.

    Input items arrive as TypedDicts (so: dicts) and output items as pydantic
    models, and both shapes appear inside ``content`` lists too. Everything
    below reads ``type`` and ``role`` to decide a message's shape, which needs
    a dict in hand; ``None`` is the caller's cue to fall back rather than guess
    at an object it cannot open.
    """
    if isinstance(item, Mapping):
        return dict(item)
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return None


def text_part(content: Any) -> dict[str, Any]:
    return {"type": "text", "content": content}


def _tool_call_part(item: dict[str, Any]) -> dict[str, Any]:
    """A ``function_call`` item as a GenAI ``tool_call`` part.

    ``arguments`` is left as the string the model emitted. Parsing it would
    change the record, and models do emit malformed JSON, which would raise.
    Redaction still reaches inside it — see ``TOOL_CALL_JSON_KEYS``.
    """
    return {
        "type": "tool_call",
        "id": item.get("call_id") or item.get("id") or "",
        "name": item.get("name", ""),
        "arguments": item.get("arguments"),
    }


def _content_parts(content: Any) -> list[dict[str, Any]]:
    """A message's ``content`` as GenAI parts.

    ``content`` is either a bare string or a list of content parts. A part
    whose payload is text becomes a ``text`` part — a refusal included, whose
    text is the interesting half. Anything else (an image, a file) is carried
    through whole: it already names itself under ``type``, and dropping it
    would lose exactly the attachment an investigator is looking for.
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [text_part(content)]
    if not isinstance(content, (list, tuple)):
        return [text_part(content)]
    parts: list[dict[str, Any]] = []
    for raw in content:
        part = _as_dict(raw)
        if part is None:
            parts.append(text_part(raw))
        elif part.get("type") in _TEXT_PART_TYPES and "text" in part:
            parts.append(text_part(part["text"]))
        elif "refusal" in part:
            parts.append(text_part(part["refusal"]))
        else:
            parts.append(part)
    return parts


def input_message(item: Any) -> dict[str, Any]:
    """One entry of ``input_items`` as a GenAI message.

    The list mixes three kinds of entry: ordinary role messages, the model's
    own ``function_call`` items, and the ``function_call_output`` items the
    runner appends once a tool returns. All three are converted — the tool
    results especially, since a decision event records that a tool was called
    but never what it returned, making this the only place that value lands.
    """
    entry = _as_dict(item)
    if entry is None:
        return {"role": "user", "parts": [text_part(item)]}
    entry_type = entry.get("type")
    if entry_type == "function_call":
        return {"role": "assistant", "parts": [_tool_call_part(entry)]}
    if entry_type == "function_call_output":
        return {
            "role": "tool",
            "parts": [
                {
                    "type": "tool_call_response",
                    "id": entry.get("call_id") or "",
                    "response": entry.get("output"),
                }
            ],
        }
    if "role" in entry:
        return {"role": entry["role"], "parts": _content_parts(entry.get("content"))}
    # Reasoning items, built-in tool calls, MCP approvals: no role and no
    # GenAI part to map onto, so carry them through whole rather than drop
    # them. "assistant" is a floor — none of these is a user message.
    return {"role": "assistant", "parts": [entry]}


def output_messages(output: list[Any]) -> list[dict[str, Any]]:
    """``ModelResponse.output`` as the single assistant message it represents.

    One model call returns one completion; the Responses API just splits it
    across several items — a text message, one item per tool call, maybe a
    reasoning item. Folding them back into one message with its parts in the
    order the model produced them is what a reader expects to see, and it
    matches the ``gen_ai.output.messages`` contract of "this call's
    completion".
    """
    parts: list[dict[str, Any]] = []
    for raw in output:
        item = _as_dict(raw)
        if item is None:
            parts.append(text_part(raw))
        elif item.get("type") == "function_call":
            parts.append(_tool_call_part(item))
        elif item.get("content"):
            parts.extend(_content_parts(item["content"]))
        else:
            # Truthiness above, not ``"content" in item``: a reasoning item
            # declares ``content`` as None and keeps its text under
            # ``summary``, so a key check would emit an empty message.
            parts.append(item)
    return [{"role": "assistant", "parts": parts}] if parts else []
