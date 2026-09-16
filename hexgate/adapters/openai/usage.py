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
last call — because re-sending the whole list every call would store the
transcript once per turn.

Which half does that thinning depends on how the run was started, and the hook
cannot tell from the input list alone:

* By default the SDK re-sends the whole conversation on every call, so the
  hook is handed the full history and
  :class:`~hexgate.tracing.messages.MessageCursor` works out what is new.
* Under a server-managed conversation (``conversation_id``,
  ``previous_response_id`` or ``auto_previous_response_id`` passed to
  ``run*``), OpenAI holds the history and the SDK sends only the un-sent
  items, so the hook is handed a delta already. Diffing it against a prefix
  that was never re-sent matches nothing and would flag every event
  ``resynced``, so ``framework_sends_deltas`` turns the diff off and the items
  go out as they arrive.

Turning the Responses API's items into GenAI messages is ``messages.py``'s
job, beside this one. What is left here is the hook pair itself.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, NamedTuple

from agents import Agent, RunConfig, RunContextWrapper
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

    ``input_items`` is exactly what the model is about to see: the whole
    conversation by default, or only the un-sent items under a server-managed
    conversation. Which one it is decides whether the cursor diffs it — see
    the module docstring.
    """

    system_prompt: str | None
    input_items: list[TResponseInputItem]


def _resolve_model(agent: Agent, run_config: RunConfig | None = None) -> str:
    """The model id to report for this call.

    Mirrors the precedence in ``agents.run_internal.turn_preparation.get_model``:
    ``run_config.model`` overrides ``agent.model`` in both its ``str`` and
    ``Model`` forms, so a run started with ``RunConfig(model=...)`` is served
    by that model whatever the agent declares. Reading ``agent.model`` alone
    would name the model that did *not* answer.

    Either may be ``str | Model | None``. None on both means the agent didn't
    set one and the runner/SDK default applies -- that default is resolved
    deep in the SDK's run loop and never reaches this hook, so "default" is an
    honest placeholder rather than a guess.
    """
    configured = run_config.model if run_config is not None else None
    model = configured if configured is not None else agent.model
    if isinstance(model, str):
        return model
    if model is None:
        return "default"
    # Standard Model impls expose the real id in .model; class name is a
    # last resort for an exotic Model that doesn't.
    return getattr(model, "model", None) or type(model).__name__


class HexgateUsageHooks(RunHooks):
    """Emits one :class:`~hexgate.tracing.usage.LlmUsageEvent` and one
    :class:`~hexgate.tracing.messages.LlmMessageEvent` per ``on_llm_end``
    callback, from the prompt stashed by the matching ``on_llm_start``.

    ``HexgateRunner`` builds a fresh instance per ``run*`` call, so the
    cursor and the stash below live exactly one run — which is how the
    cursor's "reset on run end" contract is met, raises included.
    """

    def __init__(
        self,
        *,
        api_key: str,
        framework_sends_deltas: bool = False,
        run_config: RunConfig | None = None,
    ) -> None:
        self._api_key = api_key
        # The run's own config, for the model override it may carry.
        # ``RunContextWrapper`` does not expose it (it holds context, usage,
        # turn_input and the approval/tool state), so the hook cannot recover
        # it from the callback arguments — the runner hands it over instead.
        self._run_config = run_config
        # True when the run was started under a server-managed conversation, so
        # the SDK hands this hook un-sent items rather than the whole history
        # and the cursor's diff must be skipped. The hook cannot see the
        # tracker that decides this; ``HexgateRunner`` sees the kwargs that
        # build it and passes the answer down. Defaults false: a bare
        # ``RunHooks`` user gets the full-history behaviour the SDK's own
        # default produces.
        self._framework_sends_deltas = framework_sends_deltas
        self._cursor = MessageCursor()
        # Request side of each in-flight LLM call, keyed by turn: the response
        # hook carries no prompt, so it has to be handed one.
        self._pending: dict[str, _Prompt] = {}
        # ``message_seq`` per turn when the cursor is bypassed. The cursor owns
        # this counter in the normal path; in delta mode nothing else does, and
        # a seq that restarted at 0 each call would read as a lost row.
        self._delta_seq: dict[str, int] = {}
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

        ``input_items`` is the whole conversation unless the run is under a
        server-managed conversation, in which case it is already the delta
        (see the module docstring) — and a handoff target gets the same list,
        so its first event restates what the source already logged under a new
        ``turn_key`` (issue #215).
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
        model = _resolve_model(agent, self._run_config)
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
        messages, seq, resynced = self._delta(key, new_input)
        emit_llm_messages(
            agent.name,
            model,
            messages,
            output,
            turn_key=key,
            message_seq=seq,
            # First event of each turn only — a handoff target has its own
            # instructions, and repeating them would spend the 8 KiB budget.
            system_instructions=(
                [text_part(system_prompt)] if seq == 0 and system_prompt else None
            ),
            resynced=resynced,
            api_key=self._api_key,
        )

    def _delta(self, key: str, new_input: list[Any]) -> tuple[list[Any], int, bool]:
        """What to emit for this call: the messages, their ``message_seq`` and
        whether they restate the turn's history.

        Two sources, one shape. Normally the cursor diffs the full list it was
        handed. Under a server-managed conversation the SDK already sent only
        the un-sent items, so there is nothing to diff — the list *is* the
        delta, and it goes out whole under a seq counted here.

        ``resynced`` stays false in that mode, and deliberately: the flag says
        "the rows before this one are superseded", which is exactly as untrue
        here as it is for an ordinary extension. A reader concatenating rows
        gets the transcript either way.
        """
        if not self._framework_sends_deltas:
            delta = self._cursor.advance(key, new_input)
            return delta.messages, delta.seq, delta.resynced
        seq = self._delta_seq.get(key, 0)
        self._delta_seq[key] = seq + 1
        return new_input, seq, False
