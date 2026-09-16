"""google-genai ``Content``/``Part`` → OTel GenAI ``role``/``parts`` messages.

The shape translation for the Google ADK adapter, kept apart from the
``BasePlugin`` hooks in ``usage.py``: pure functions that decide nothing about
when or whether to emit. The target shape is the official GenAI one — a
message is ``{"role": …, "parts": [...]}`` and a part names itself under
``type`` — which is what ``hexgate.tracing.messages.LlmMessageEvent`` puts on
the wire.

Nothing is hoisted into a shared module yet even though this is the second
adapter (§III SDK leaves that open): ADK hands over typed pydantic
``Content``/``Part`` objects read by attribute, so none of the OpenAI
adapter's dict-probing helpers repeat here — only the one-line
:func:`text_part`.
"""

from __future__ import annotations

from typing import Any

from google.genai import types


def text_part(content: Any) -> dict[str, Any]:
    return {"type": "text", "content": content}


def _part(part: types.Part) -> dict[str, Any]:
    """One ``Part`` as a GenAI part.

    Anything without a GenAI equivalent (inline data, a file reference,
    executable code) is carried through whole rather than dropped, since the
    transcript exists to explain a run after the fact — but tagged on the way
    out: a ``Part`` is a union by populated field, so unlike the OpenAI
    adapter's items it does not already name itself under ``type``.
    """
    if part.function_call is not None:
        call = part.function_call
        return {
            "type": "tool_call",
            "id": call.id or "",
            "name": call.name or "",
            # Left as the dict google-genai parsed it into; redaction reaches
            # inside it the same way it does the decision event's arguments.
            "arguments": call.args,
        }
    if part.function_response is not None:
        response = part.function_response
        return {
            "type": "tool_call_response",
            "id": response.id or "",
            "name": response.name or "",
            "response": response.response,
        }
    if part.text is not None:
        # A thought stays self-identifying rather than flattening to text: ADK
        # carries it forward in the history, where it would otherwise read as
        # the answer the model gave.
        if part.thought:
            return {"type": "reasoning", "content": part.text}
        return text_part(part.text)
    carried = part.model_dump(mode="json", exclude_none=True)
    return {"type": next(iter(carried), "unknown"), **carried}


def _role(content: types.Content) -> str:
    """GenAI role for one ``Content``.

    ADK files a tool result under ``role="user"`` because that is what the
    Gemini API wants; GenAI calls that turn ``tool``, and the parts inside say
    which it is.
    """
    if any(part.function_response is not None for part in content.parts or []):
        return "tool"
    if content.role == "model":
        return "assistant"
    return content.role or "user"


def input_message(content: types.Content) -> dict[str, Any]:
    """One entry of ``LlmRequest.contents`` as a GenAI message.

    Tool results are converted like everything else: a decision event records
    that a tool was called but never what it returned, so this is the only
    place that value lands.
    """
    return {
        "role": _role(content),
        "parts": [_part(part) for part in content.parts or []],
    }


def output_messages(content: types.Content | None) -> list[dict[str, Any]]:
    """``LlmResponse.content`` as the single assistant message it represents.

    Thought parts are dropped, as reasoning is on the OpenAI side (issue
    #221): every part here shares one 8 KiB budget, and head+tail truncation
    favours the head, so a long chain of thought would be cut out of the
    answer rather than out of itself.
    """
    parts = (
        [_part(p) for p in (content.parts or []) if not p.thought] if content else []
    )
    return [{"role": "assistant", "parts": parts}] if parts else []


def system_parts(instruction: Any) -> list[dict[str, Any]] | None:
    """``GenerateContentConfig.system_instruction`` as GenAI parts.

    google-genai types it as a ``ContentUnion`` — a bare string, a ``Content``,
    a ``Part``, or a list mixing those — so a caller-supplied
    ``generate_content_config`` can carry any of them.
    """
    if instruction is None:
        return None
    if isinstance(instruction, str):
        return [text_part(instruction)]
    if isinstance(instruction, types.Part):
        return [_part(instruction)]
    if isinstance(instruction, types.Content):
        return [_part(part) for part in instruction.parts or []] or None
    if isinstance(instruction, (list, tuple)):
        parts: list[dict[str, Any]] = []
        for item in instruction:
            parts.extend(system_parts(item) or [])
        return parts or None
    return [text_part(str(instruction))]
