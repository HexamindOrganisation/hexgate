"""A turn's stream as AG-UI events: the names and payload fields are
AG-UI's own, serialised as one server-sent event each."""

import json
import uuid
from collections.abc import Iterator
from typing import Any

from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
)

from hexgate_assistant.changes import redact, redact_retry
from hexgate_assistant.context import keep_ends

Event = dict[str, Any]

# A tool result as the browser receives it, start and end kept. Smaller than
# what the model may receive whole (a policy file or reference section up to
# 64 KiB), larger than every other result the model gets.
BROWSER_RESULT_BYTES = 32 * 1024

# The keep-alive: a comment line, which every SSE client skips.
PING = ": ping\n\n"


def sse(event: Event) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


def run_started(thread_id: str, run_id: str) -> Event:
    return {"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id}


def run_finished(thread_id: str, run_id: str) -> Event:
    return {"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id}


def custom(name: str, value: Any) -> Event:
    return {"type": "CUSTOM", "name": name, "value": value}


def run_error(code: str | None, message: str) -> Event:
    return {"type": "RUN_ERROR", "message": message, "code": code}


class EventTranslator:
    """Pydantic AI's run events, one at a time, as AG-UI events. Thinking
    and every other kind of part are left out."""

    def __init__(self) -> None:
        # The AG-UI message id of each text part being streamed, by its
        # index in the model's response.
        self._texts: dict[int, str] = {}
        # The tool call id of each call being streamed, by the same index.
        # Its arguments are held until the call is complete: only then can
        # they be told apart from whole files (see `changes.redact`).
        self._calls: dict[int, str] = {}

    def translate(self, event: AgentStreamEvent) -> Iterator[Event]:
        if isinstance(event, PartStartEvent):
            yield from self._start(event)
        elif isinstance(event, PartDeltaEvent):
            yield from self._delta(event)
        elif isinstance(event, PartEndEvent):
            yield from self._end(event)
        elif isinstance(event, FunctionToolResultEvent):
            yield self._result(event)

    def _start(self, event: PartStartEvent) -> Iterator[Event]:
        part = event.part
        if isinstance(part, TextPart):
            message_id = str(uuid.uuid4())
            self._texts[event.index] = message_id
            yield {
                "type": "TEXT_MESSAGE_START",
                "messageId": message_id,
                "role": "assistant",
            }
            if part.content:
                yield _text(message_id, part.content)
        elif isinstance(part, ToolCallPart):
            self._calls[event.index] = part.tool_call_id
            yield {
                "type": "TOOL_CALL_START",
                "toolCallId": part.tool_call_id,
                "toolCallName": part.tool_name,
            }

    def _delta(self, event: PartDeltaEvent) -> Iterator[Event]:
        delta = event.delta
        if isinstance(delta, TextPartDelta) and event.index in self._texts:
            if delta.content_delta:
                yield _text(self._texts[event.index], delta.content_delta)

    def _end(self, event: PartEndEvent) -> Iterator[Event]:
        if (message_id := self._texts.pop(event.index, None)) is not None:
            yield {"type": "TEXT_MESSAGE_END", "messageId": message_id}
        elif (call_id := self._calls.pop(event.index, None)) is not None:
            if isinstance(event.part, ToolCallPart) and event.part.args:
                yield _args(call_id, _shown_args(event.part))
            yield {"type": "TOOL_CALL_END", "toolCallId": call_id}

    def _result(self, event: FunctionToolResultEvent) -> Event:
        part = redact_retry(event.part)
        return {
            "type": "TOOL_CALL_RESULT",
            "messageId": str(uuid.uuid4()),
            "toolCallId": part.tool_call_id,
            "content": keep_ends(
                part.model_response_str(),
                BROWSER_RESULT_BYTES,
                "\n[… {cut} bytes not shown …]\n",
            ),
            "role": "tool",
        }


def _shown_args(part: ToolCallPart) -> str:
    """A complete call's arguments as the browser is sent them: as the model
    wrote them (raw text, possibly cut short), or without file text."""
    args = part.args_as_dict()
    shown = redact(part.tool_name, args)
    if shown is not args:
        return json.dumps(shown)
    return part.args if isinstance(part.args, str) else part.args_as_json_str()


def _text(message_id: str, delta: str) -> Event:
    return {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": delta}


def _args(call_id: str, delta: str) -> Event:
    return {"type": "TOOL_CALL_ARGS", "toolCallId": call_id, "delta": delta}
