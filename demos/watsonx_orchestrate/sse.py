"""Translate between watsonx Orchestrate's ``external_chat`` contract and Hexgate.

Framework-free (no FastAPI import) so the mapping is unit-testable on its own.

Contract source: ``external_agent/spec.yaml`` and ``external_agent/README.md`` in
https://github.com/watson-developer-cloud/watsonx-orchestrate-developer-toolkit.
Orchestrate streams server-sent events, one ``data: <json>`` frame each:

* ``thread.message.delta`` — answer text shown to the user;
* ``thread.run.step.delta`` — chain-of-thought steps (``thinking``,
  ``tool_calls``, ``tool_response``); a ``tool_response``'s ``tool_call_id``
  must match its ``tool_calls[].id`` or the Orchestrate UI mis-renders the steps;
* ``data: [DONE]`` — end of stream.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from hexgate.streaming import (
    BlockDeltaEvent,
    BlockType,
    ErrorEvent,
    StreamEvent,
    ToolEndEvent,
    ToolStartEvent,
)

DONE = "data: [DONE]\n\n"

# Roles the OpenAI Agents SDK accepts as plain ``{"role", "content"}`` input items.
# Orchestrate may also send ``tool`` messages and assistant ``tool_calls`` from its
# own turns; those have no meaning to our agent, so they are dropped.
_INPUT_ROLES = {"user", "assistant", "system"}


def _text(content: Any) -> str:
    """Text of a message's ``content``: a string, or OpenAI-style content parts
    (``[{"type": "text", "text": ...}]``) joined with newlines. Other part types
    and shapes yield ``""``."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
        return "\n".join(parts).strip()
    return ""


def to_agent_input(messages: list[Any]) -> list[dict[str, str]]:
    """Map Orchestrate's ``messages`` to OpenAI Agents SDK input items.

    Keeps user/assistant/system turns that carry text; skips anything else
    (tool turns, empty content, non-dict entries).
    """
    items: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in _INPUT_ROLES:
            continue
        text = _text(message.get("content"))
        if text:
            items.append({"role": message["role"], "content": text})
    return items


def last_user_message(messages: list[Any]) -> str:
    """Text of the latest user turn (the run's ``query``), or ``""``.

    Looks only at the latest user turn: when it carries no text this returns
    ``""`` rather than an older turn, so the caller can reject the request
    instead of silently answering a previous message.
    """
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return _text(message.get("content"))
    return ""


def _frame(obj: str, thread_id: str, model: str, delta: dict[str, Any]) -> str:
    payload = {
        "id": f"evt-{uuid.uuid4().hex[:12]}",
        "object": obj,
        "thread_id": thread_id,
        "model": model,
        "created": int(time.time()),
        "choices": [{"delta": {"role": "assistant", **delta}}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def to_sse(event: StreamEvent, *, thread_id: str, model: str) -> str | None:
    """Render one Hexgate ``StreamEvent`` as an Orchestrate SSE frame.

    Returns ``None`` for events Orchestrate has no frame for (run start/end,
    block boundaries, intermediate tool updates).
    """
    if isinstance(event, BlockDeltaEvent):
        if event.block_type == BlockType.REASONING:
            return _frame(
                "thread.run.step.delta",
                thread_id,
                model,
                {"step_details": {"type": "thinking", "content": event.text}},
            )
        return _frame("thread.message.delta", thread_id, model, {"content": event.text})
    if isinstance(event, ToolStartEvent):
        return _frame(
            "thread.run.step.delta",
            thread_id,
            model,
            {
                "step_details": {
                    "type": "tool_calls",
                    "tool_calls": [
                        {
                            "id": event.tool_id,
                            "name": event.tool_name,
                            "args": event.arguments,
                        }
                    ],
                }
            },
        )
    if isinstance(event, ToolEndEvent):
        return _frame(
            "thread.run.step.delta",
            thread_id,
            model,
            {
                "step_details": {
                    "type": "tool_response",
                    "name": event.tool_name,
                    "tool_call_id": event.tool_id,
                    "content": event.output_summary or "",
                }
            },
        )
    if isinstance(event, ErrorEvent):
        return _frame(
            "thread.message.delta",
            thread_id,
            model,
            {"content": f"\n\n[error] {event.message}"},
        )
    return None


def completion_body(content: str, *, model: str) -> dict[str, Any]:
    """Non-streaming ``chat.completion`` response body (``stream: false``)."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
