"""pydantic_ai ``ModelMessage``s → OTel GenAI ``role``/``parts`` messages.

The shape translation for the pydantic_ai adapter, kept apart from the emit in
``usage.py`` (as in ``adapters/openai/messages.py``): pure functions over the
framework's message objects that decide nothing about when or whether to emit.

Unlike the other adapters this one converts a whole run at once, because
pydantic_ai has no per-call hook short of ``iter()``. The completion is the
*last* ``ModelResponse`` in the history, not the last message: a run with an
``output_type`` delivers its answer through a ``final_result`` tool call, so
the history ends on the ``ModelRequest`` carrying that tool's return.

An unrecognised part is carried through whole rather than dropped, since the
transcript exists to explain a run after the fact — with one exception:
``ThinkingPart`` is dropped from *both* sides. Issue #221 drops it from the
completion, where it shares one 8 KiB budget with the answer; here the input is
the whole run rather than one call's delta, so every past response's reasoning
would ride in it too.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse

from hexgate.adapters._messages import text_part

_DROPPED_KINDS = frozenset({"thinking"})

_TEXT_KINDS = frozenset({"text", "system-prompt"})

_TOOL_CALL_KINDS = frozenset({"tool-call", "builtin-tool-call"})

_TOOL_RETURN_KINDS = frozenset({"tool-return", "builtin-tool-return"})

# Response parts carry no role of their own — they are the assistant's.
_ROLES = {
    "system-prompt": "system",
    "user-prompt": "user",
    "tool-return": "tool",
    "builtin-tool-return": "tool",
}


def _kind(part: Any) -> str:
    return getattr(part, "part_kind", "")


def _role(part: Any) -> str:
    kind = _kind(part)
    if kind == "retry-prompt":
        # A retry with no tool name is validation feedback on the model's own
        # text output, not a tool result.
        return "tool" if getattr(part, "tool_name", None) else "user"
    return _ROLES.get(kind, "assistant")


def _media_part(value: Any) -> dict[str, Any] | None:
    """A descriptor for anything holding raw bytes, or ``None``.

    Carried through whole, ``bytes`` serialise to an escaped repr four times
    their own size: one inlined image fills the whole 256 KiB input budget and
    caps away every message beside it, to store something nobody can read.
    """
    binary = value if hasattr(value, "data") else getattr(value, "content", None)
    data = getattr(binary, "data", None)
    if not isinstance(data, (bytes, bytearray)):
        return None
    return {
        "type": "binary",
        "media_type": getattr(binary, "media_type", ""),
        "bytes": len(data),
    }


def _media_content(value: Any) -> Any:
    """Tool-return content with any inline bytes reduced to a descriptor. A
    tool returning a screenshot hits the same 4x escaped-repr expansion as a
    user prompt carrying one — see :func:`_media_part`."""
    if isinstance(value, (list, tuple)):
        return [_media_part(item) or item for item in value]
    return _media_part(value) or value


def _user_parts(content: Any) -> list[dict[str, Any]]:
    """A ``UserPromptPart``'s content, which is a string or a mixed sequence."""
    items = [content] if isinstance(content, str) else list(content)
    parts: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            parts.append(text_part(item))
        elif getattr(item, "kind", "") == "text-content":
            parts.append(text_part(item.content))
        else:
            parts.append(
                _media_part(item)
                or {"type": getattr(item, "kind", "content"), "part": item}
            )
    return parts


def part_to_genai(part: Any) -> list[dict[str, Any]]:
    """One message part as GenAI parts — empty when the part is dropped.

    Tool returns are converted too: a decision event records that a tool was
    called but never what it returned, making this the only place that lands.
    """
    kind = _kind(part)
    if kind in _DROPPED_KINDS:
        return []
    if kind in _TEXT_KINDS:
        return [text_part(part.content)]
    if kind == "user-prompt":
        return _user_parts(part.content)
    if kind in _TOOL_CALL_KINDS:
        return [
            {
                "type": "tool_call",
                "id": part.tool_call_id or "",
                "name": part.tool_name,
                # Left as pydantic_ai handed it: a JSON string from the model,
                # or an already-parsed dict. Redaction reaches inside both.
                "arguments": part.args,
            }
        ]
    if kind in _TOOL_RETURN_KINDS or kind == "retry-prompt":
        response: dict[str, Any] = {
            "type": "tool_call_response",
            "id": part.tool_call_id or "",
            "name": getattr(part, "tool_name", None) or "",
            "response": _media_content(part.content),
        }
        if kind == "retry-prompt":
            response["error"] = True
        return [response]
    return [_media_part(part) or {"type": kind or "part", "part": part}]


def _messages(parts: Iterable[Any]) -> list[dict[str, Any]]:
    """Parts as GenAI messages, consecutive parts of one role folded together
    — a request holding three parallel tool returns is one ``tool`` message."""
    messages: list[dict[str, Any]] = []
    for part in parts:
        converted = part_to_genai(part)
        if not converted:
            continue
        role = _role(part)
        if messages and messages[-1]["role"] == role:
            messages[-1]["parts"].extend(converted)
        else:
            messages.append({"role": role, "parts": converted})
    return messages


def last_response(history: list[ModelMessage]) -> ModelResponse | None:
    """The run's completion: the last ``ModelResponse``, which is not always
    the last message (see the module docstring). Also names the model that
    answered, which ``AgentRun`` exposes nowhere else."""
    for message in reversed(history):
        if isinstance(message, ModelResponse):
            return message
    return None


def run_messages(
    history: list[ModelMessage],
    *,
    completed: bool = True,
    session: list[ModelMessage] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]] | None]:
    """A run's messages as ``(input, output, system_instructions)``.

    The completion is the last ``ModelResponse`` — see the module docstring;
    everything else is input, in the order it happened.

    ``completed=False`` says the run never produced an answer, so nothing is
    hoisted: the last ``ModelResponse`` of a run cut short is a mid-run tool
    call, and promoting it would claim the model answered with a tool call and
    strand the matching tool return in the input without it. Every message
    stays in ``input``, and an empty completion is the truthful record.

    System content is lifted out of the messages because the wire contract
    carries it in its own field. ``session`` is the whole conversation when the
    run was started from a ``message_history``: a ``system_prompt=`` lands only
    in the very first request of a conversation, so ``history`` — this run's
    own messages — does not carry it from turn two onward.

    The ``instructions`` reported are the ones the completion was produced
    under: the last request *before* it, taken verbatim including ``None``.
    pydantic_ai re-renders instructions per request, so a conditional
    ``@agent.instructions`` that returns a policy on the first call and nothing
    after would otherwise have the row claim a guardrail that was not in force.
    """
    completion = last_response(history) if completed else None
    system: list[dict[str, Any]] = []
    instructions: str | None = None
    input_messages: list[dict[str, Any]] = []
    for message in session if session is not None else history:
        if isinstance(message, ModelRequest):
            system.extend(
                text_part(part.content)
                for part in message.parts
                if _kind(part) == "system-prompt"
            )
    for message in history:
        if message is completion:
            break
        if isinstance(message, ModelRequest):
            instructions = message.instructions
    for message in history:
        if message is completion:
            continue
        parts = [part for part in message.parts if _kind(part) != "system-prompt"]
        input_messages.extend(_messages(parts))
    if instructions:
        system.insert(0, text_part(instructions))
    output = _messages(completion.parts) if completion is not None else []
    return input_messages, output, system or None
