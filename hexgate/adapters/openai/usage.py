"""OpenAI Agents SDK per-call token usage and message capture via ``RunHooks``.

``Runner.run``/``run_sync``/``run_streamed`` invoke ``on_llm_start`` and
``on_llm_end`` once each per underlying model call, so a single run with
several turns (tool-calling loops, handoffs) can emit more than one usage and
message event.

The two hooks come as a pair because neither half carries the whole call:
``on_llm_start`` sees the prompt (``system_prompt`` plus the entire input
list), ``on_llm_end`` sees the completion and the token counts. The request
side is stashed under the turn key and picked up when the response lands, so
usage and messages leave from one call site.

What reaches the wire is the *delta* — only the items added since this turn's
last call — because the input list is the whole conversation so far and
re-sending it every call would store the transcript once per turn. Working out
that delta is :class:`~hexgate.tracing.messages.MessageCursor`'s job, written
to be shared by all four adapters (this is the first to use it); this module's
own work is the shape translation from the Responses API's items to the OTel
GenAI ``role``/``parts`` message form.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

from agents import Agent, RunContextWrapper
from agents.items import ModelResponse
from agents.lifecycle import RunHooks

from hexgate.runtime.run_facts import get_run_facts
from hexgate.tracing.messages import (
    MessageCursor,
    emit_llm_messages,
    log_messages_enabled,
)
from hexgate.tracing.usage import emit_llm_usage

_log = logging.getLogger(__name__)

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


def _text_part(content: Any) -> dict[str, Any]:
    return {"type": "text", "content": content}


def _tool_call_part(item: dict[str, Any]) -> dict[str, Any]:
    """A ``function_call`` item as a GenAI ``tool_call`` part.

    ``arguments`` stays the raw JSON *string* the API uses rather than being
    parsed here: ``TOOL_CALL_JSON_KEYS`` makes redaction open that string and
    blank the sensitive keys inside it, so an api_key in a tool's arguments is
    masked in the transcript exactly as it is on the decision event.
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
        return [_text_part(content)]
    if not isinstance(content, (list, tuple)):
        return [_text_part(content)]
    parts: list[dict[str, Any]] = []
    for raw in content:
        part = _as_dict(raw)
        if part is None:
            parts.append(_text_part(raw))
        elif part.get("type") in _TEXT_PART_TYPES and "text" in part:
            parts.append(_text_part(part["text"]))
        elif "refusal" in part:
            parts.append(_text_part(part["refusal"]))
        else:
            parts.append(part)
    return parts


def _input_message(item: Any) -> dict[str, Any]:
    """One entry of ``input_items`` as a GenAI message.

    The list mixes three kinds of entry: ordinary role messages, the model's
    own ``function_call`` items, and the ``function_call_output`` items the
    runner appends once a tool returns. All three are converted — the tool
    results especially, since a decision event records that a tool was called
    but never what it returned, making this the only place that value lands.
    """
    entry = _as_dict(item)
    if entry is None:
        return {"role": "user", "parts": [_text_part(item)]}
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
    # Reasoning items, built-in tool calls (web search, computer use), MCP
    # approval requests: no role of their own, and no GenAI part type to map
    # onto. Carried through under the role that produced them so the turn is
    # still complete, rather than dropped for want of a mapping. The assistant
    # role is a floor, not a reading of the item: none of these is a user
    # message, and GenAI has no role for "the framework did this".
    return {"role": "assistant", "parts": [entry]}


def _output_messages(output: list[Any]) -> list[dict[str, Any]]:
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
            parts.append(_text_part(raw))
        elif item.get("type") == "function_call":
            parts.append(_tool_call_part(item))
        elif item.get("content"):
            parts.extend(_content_parts(item["content"]))
        else:
            # Truthiness, not ``"content" in item``: a reasoning item declares
            # a ``content`` field that is almost always None and keeps its text
            # under ``summary``, so a key check would route it here and emit an
            # empty message — losing the last turn's reasoning entirely, since
            # only earlier turns reappear in the next call's input delta.
            parts.append(item)
    return [{"role": "assistant", "parts": parts}] if parts else []


def _resolve_model(agent: Agent) -> str:
    """The model id to report for ``agent``.

    ``agent.model`` is ``str | Model | None``. None means the agent didn't set
    one and the runner/SDK default applies -- that default is resolved deep in
    the SDK's run loop and never reaches this hook, so "default" is an honest
    placeholder rather than a guess.
    """
    if isinstance(agent.model, str):
        return agent.model
    if agent.model is None:
        return "default"
    # Standard Model impls expose the real id in .model; class name is a
    # last resort for an exotic Model that doesn't.
    return getattr(agent.model, "model", None) or type(agent.model).__name__


class HexgateUsageHooks(RunHooks):
    """Emits one :class:`~hexgate.tracing.usage.LlmUsageEvent` and one
    :class:`~hexgate.tracing.messages.LlmMessageEvent` per ``on_llm_end``
    callback, from the prompt stashed by the matching ``on_llm_start``.

    This is where the cursor's "reset on run end" contract is met: only
    ``HexgateRunner`` constructs this class, and it builds a fresh instance
    inside every ``run*`` call, so the cursor and the stash below are born and
    die with one run — a stronger guarantee than resetting keys one by one,
    and unlike an explicit reset it still holds when the run raises. Nothing
    is evicted from ``on_agent_end`` for that reason, and because that hook
    fires only for the agent that produced the final output, leaving a
    handoff source's list behind.
    """

    def __init__(self, *, api_key: str) -> None:
        self._api_key = api_key
        self._cursor = MessageCursor()
        # Request side of each in-flight LLM call, keyed by turn: the response
        # hook carries no prompt, so it has to be handed one.
        self._pending: dict[str, tuple[str | None, list[Any]]] = {}
        # Stands in for the run id when there is no run scope (see _turn_key).
        self._fallback_run_id = uuid.uuid4().hex

    def _turn_key(self, agent: Agent) -> str:
        """Identity of the *message list* this call extends.

        Hexgate's own run id, not ``id(context)``: the run context object is
        freed when the run ends and CPython hands the next run the same
        address, so in a process that serves many runs — ``hexgate serve``, a
        chat session — two unrelated conversations would land under one
        ``turn_key``, each restarting ``message_seq`` at 0. That is the one
        thing the key exists to prevent, and nothing downstream would notice:
        the rows insert cleanly and only the gap detection quietly stops
        meaning anything. The run id is minted per ``run*`` call and is on the
        row anyway as ``run_id``.

        Falls back to the instance id when there is no run scope, which
        ``HexgateRunner`` always opens — the fallback is for a bare
        ``Runner.run`` given these hooks directly, where a per-instance
        constant is still unique to one hooks object.

        The agent name is the second half because one run can drive several
        agents. See the note in :meth:`on_llm_start` on what that costs after
        a handoff.
        """
        return f"{get_run_facts().id or self._fallback_run_id}:{agent.name}"

    async def on_llm_start(
        self,
        context: RunContextWrapper,
        agent: Agent,
        system_prompt: str | None,
        input_items: list[Any],
    ) -> None:
        """Stash the prompt for the matching ``on_llm_end``.

        ``input_items`` is the whole conversation the model is about to see,
        not a delta. After a handoff that is also true of the *target* agent's
        first call — this SDK passes the accumulated list straight through
        unless the handoff sets an ``input_filter`` — so the target's list
        restates what the source already logged, under its own ``turn_key`` at
        seq 0. That duplication is the cost of keying per agent, and keying
        per run instead would lose a sub-agent's separate list; the spec
        picks per agent, and no content is dropped either way.
        """
        if not log_messages_enabled():
            return
        # Copied: the runner appends to this list as the turn proceeds, and
        # the delta must be measured against the prompt actually sent.
        self._pending[self._turn_key(agent)] = (
            system_prompt,
            list(input_items),
        )

    async def on_llm_end(
        self,
        context: RunContextWrapper,
        agent: Agent,
        response: ModelResponse,
    ) -> None:
        model = _resolve_model(agent)
        emit_llm_usage(
            agent.name,
            model,
            response.usage.input_tokens,
            response.usage.output_tokens,
            api_key=self._api_key,
        )
        self._emit_messages(agent, model, response)

    def _emit_messages(
        self,
        agent: Agent,
        model: str,
        response: ModelResponse,
    ) -> None:
        """Convert this call's prompt delta and completion and emit them.

        Guarded: ``emit_llm_messages`` and ``MessageCursor.advance`` never
        raise, but the conversion in front of them walks framework data and
        runs from a hook the SDK re-raises out of. Losing a transcript row
        must not fail the run it was logging — and the usage event above has
        already left.
        """
        if not log_messages_enabled():
            return
        key = self._turn_key(agent)
        system_prompt, input_items = self._pending.pop(key, (None, []))
        try:
            new_input = [_input_message(item) for item in input_items]
            output = _output_messages(response.output)
        except Exception:
            # Before ``advance``, so a failed conversion costs this event and
            # nothing else. Asking the cursor first would spend the turn's seq
            # on an event that never goes out, and a reader is specified to
            # read that hole as a lost row.
            _log.exception("converting LLM messages raised; dropping this event")
            return
        delta = self._cursor.advance(key, new_input)
        emit_llm_messages(
            agent.name,
            model,
            delta.messages,
            output,
            turn_key=key,
            message_seq=delta.seq,
            # Only on the first event of the turn: an agent reached by a
            # handoff has its own instructions, so this rides on each list's
            # first event rather than the session's, and repeating it every
            # call would spend the 8 KiB budget on the same text over and over.
            system_instructions=(
                [_text_part(system_prompt)]
                if delta.seq == 0 and system_prompt
                else None
            ),
            resynced=delta.resynced,
            api_key=self._api_key,
        )
