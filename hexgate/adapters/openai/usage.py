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
to be shared by all four adapters (this is the first to use it); turning the
Responses API's items into GenAI messages is ``messages.py``'s, beside this
one. What is left here is the hook pair itself.
"""

from __future__ import annotations

import logging
import uuid
from typing import NamedTuple

from agents import Agent, RunContextWrapper
from agents.items import ModelResponse, TResponseInputItem
from agents.lifecycle import RunHooks

from hexgate.adapters.openai.messages import (
    input_message,
    output_messages,
    text_part,
)
from hexgate.runtime.run_facts import get_run_facts
from hexgate.tracing.messages import (
    MessageCursor,
    emit_llm_messages,
    log_messages_enabled,
)
from hexgate.tracing.usage import emit_llm_usage

_log = logging.getLogger(__name__)


class _Prompt(NamedTuple):
    """What ``on_llm_start`` saw, held until the matching ``on_llm_end``.

    ``input_items`` is the whole conversation the model is about to see, not
    a delta; working out what is new is the cursor's job.
    """

    system_prompt: str | None
    input_items: list[TResponseInputItem]


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

    ``HexgateRunner`` builds a fresh instance per ``run*`` call, so the
    cursor and the stash below live exactly one run — which is how the
    cursor's "reset on run end" contract is met, raises included.
    """

    def __init__(self, *, api_key: str) -> None:
        self._api_key = api_key
        self._cursor = MessageCursor()
        # Request side of each in-flight LLM call, keyed by turn: the response
        # hook carries no prompt, so it has to be handed one.
        self._pending: dict[str, _Prompt] = {}
        # Stands in for the run id when there is no run scope (see _turn_key).
        self._fallback_run_id = uuid.uuid4().hex

    def _turn_key(self, agent: Agent) -> str:
        """Identity of the *message list* this call extends.

        The run id, not ``id(context)``: that address is reused once the run
        context is freed, so two unrelated runs would share a ``turn_key``
        and both restart ``message_seq`` at 0. The per-instance fallback
        covers a bare ``Runner.run``, which opens no run scope.
        """
        return f"{get_run_facts().id or self._fallback_run_id}:{agent.name}"

    async def on_llm_start(
        self,
        context: RunContextWrapper,
        agent: Agent,
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        """Stash the prompt for the matching ``on_llm_end``.

        ``input_items`` is the whole conversation, not a delta — and a
        handoff target gets it too, so its first event restates what the
        source already logged under a new ``turn_key`` (issue #215).
        """
        if not log_messages_enabled():
            return
        self._pending[self._turn_key(agent)] = _Prompt(
            system_prompt,
            # Copied: the runner appends to this list as the turn proceeds,
            # and the delta must be measured against the prompt actually sent.
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

        Guarded because the SDK re-raises out of this hook: losing a
        transcript row must not fail the run it was logging.
        """
        if not log_messages_enabled():
            return
        key = self._turn_key(agent)
        system_prompt, input_items = self._pending.pop(key, _Prompt(None, []))
        # Convert before advancing: a failed conversion must not spend the
        # turn's seq on an event that never goes out.
        try:
            new_input = [input_message(item) for item in input_items]
            output = output_messages(response.output)
        except Exception:
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
            # First event of each turn only — a handoff target has its own
            # instructions, and repeating them would spend the 8 KiB budget.
            system_instructions=(
                [text_part(system_prompt)] if delta.seq == 0 and system_prompt else None
            ),
            resynced=delta.resynced,
            api_key=self._api_key,
        )
