"""LangChain per-call token usage and message capture via a ``BaseCallbackHandler``.

LangGraph propagates ``config["callbacks"]`` through every node, so one
``.invoke()`` emits one usage and one message event per LLM turn in the run.
The callbacks come as a pair because neither half carries the whole call:
``on_chat_model_start`` sees the prompt, ``on_llm_end`` the completion and the
token counts, so the request side is stashed under LangChain's ``run_id`` until
the response lands. What reaches the wire is the *delta*, worked out by
:class:`~hexgate.tracing.messages.MessageCursor`, converted by ``messages.py``.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult

from hexgate.adapters.langchain.messages import (
    input_message,
    output_messages,
    split_system_instructions,
)
from hexgate.runtime.run_facts import get_run_facts
from hexgate.tracing.messages import (
    MessageCursor,
    emit_llm_messages,
    log_messages_enabled,
)
from hexgate.tracing.usage import emit_llm_usage

_log = logging.getLogger(__name__)

# Last resort when neither the response nor the request named a model, since
# the platform rejects an empty one outright (``min_length=1``) and would drop
# the whole event over its least interesting field. Tried after
# ``_started_model``, because every "default" row is one more known model
# collapsed into a single bucket on the usage breakdown.
_UNKNOWN_MODEL = "default"


class _Prompt(NamedTuple):
    """What a start callback saw, held until the matching ``on_llm_end``.

    ``messages`` is the whole conversation rather than a delta, ``turn_key``
    lets :meth:`HexgateUsageCallbackHandler.end_run` sweep one run without
    touching a concurrent one, and ``model`` is here because the request side
    is the only half that always knows it.
    """

    turn_key: str
    messages: list[BaseMessage | str]
    model: str


class HexgateUsageCallbackHandler(BaseCallbackHandler):
    """Emits one :class:`~hexgate.tracing.usage.LlmUsageEvent` and one
    :class:`~hexgate.tracing.messages.LlmMessageEvent` per ``on_llm_end``
    callback, from the prompt stashed by the matching start callback.

    Usage reads the standardized ``UsageMetadata`` and falls back to the legacy
    ``llm_output["token_usage"]``, emitting nothing when neither is present
    because a provider that reports no usage must not synthesize a zeroed
    event. One instance serves every call a proxy makes, unlike the OpenAI
    adapter's per-run hooks, hence the run-keyed cursor and :meth:`end_run`.
    """

    def __init__(self, *, agent_name: str, api_key: str | None = None) -> None:
        self._agent_name = agent_name
        self._api_key = api_key
        self._cursor = MessageCursor()
        # Request side of each in-flight LLM call, keyed by LangChain's run_id:
        # the response callback carries no prompt, so it has to be handed one.
        self._pending: dict[UUID, _Prompt] = {}

    # --- Identity -----------------------------------------------------------

    def turn_key(self, run_id: UUID | None = None) -> str:
        """Identity of the *message list* this call extends.

        The Hexgate run id, not LangChain's ``run_id`` or ``parent_run_id``,
        which are minted per callback and would restart ``message_seq`` at 0
        every turn. Knowingly coarser than one message list — a run whose
        summariser node or unwrapped sub-graph holds a second list files it
        here too, and the two then resync against each other — because nothing
        on the callback surface identifies a list. Outside a run scope each
        call becomes its own list, verbose but never two conversations in one.
        """
        run = get_run_facts().id
        return f"{run or f'lc-{run_id}'}:{self._agent_name}"

    def end_run(self, turn_key: str) -> None:
        """Forget one finished run's cursor state and any stash it left behind.

        The key is passed in, never re-read here: ``astream`` puts this call
        inside an async generator that an early ``break`` finalizes in a
        *different* Context, where re-deriving it would name whichever run is
        bound then and let an abandoned run reset a live one mid-run. Swept by
        ``turn_key`` rather than cleared, since one handler serves concurrent
        runs — which leaves ~200 bytes per *nested* run uncollected, closable
        only by changing the cursor's eviction contract.
        """
        self._cursor.reset(turn_key)
        for run_id, prompt in list(self._pending.items()):
            if prompt.turn_key == turn_key:
                self._pending.pop(run_id, None)

    # --- Request side -------------------------------------------------------

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Stash the prompt for the matching ``on_llm_end``, where
        ``messages[0]`` is the whole conversation (one list per prompt).

        Stashed whether or not message logging is on, because the usage event
        reads the model off this too and that stream has nothing to do with
        ``HEXGATE_LOG_MESSAGES``; only the conversation is gated, so an
        opted-out process never pays to copy a prompt it will not keep.
        """
        self._pending[run_id] = _Prompt(
            self.turn_key(run_id),
            # Copied: LangChain hands over the live list, which the chain goes
            # on to append to, and the delta must be measured against the
            # prompt actually sent.
            list(messages[0]) if (messages and log_messages_enabled()) else [],
            _started_model(metadata, kwargs),
        )

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """The same stash for a non-chat LLM, whose prompts are bare strings;
        LangChain falls back here only when the override above is missing."""
        self._pending[run_id] = _Prompt(
            self.turn_key(run_id),
            list(prompts) if log_messages_enabled() else [],
            _started_model(metadata, kwargs),
        )

    async def on_llm_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        """Drop the stash of a call that failed, since no ``on_llm_end`` will;
        the cursor is left untouched so the next call still measures its delta
        against the last prompt that reached a model."""
        self._pending.pop(run_id, None)

    # --- Response side ------------------------------------------------------

    async def on_llm_end(
        self, response: LLMResult, *, run_id: UUID, **kwargs: Any
    ) -> None:
        # Async so LangChain awaits this inline on the real event loop (its
        # iscoroutinefunction check) rather than on a thread pool, where the
        # sender gets no loop and every event is silently dropped.
        # Popped here, not in _emit_messages, because the usage event needs the
        # model off it and a stash left behind with logging off is never swept.
        prompt = self._pending.pop(run_id, None)
        model = (
            _model_name(response) or (prompt.model if prompt else "") or _UNKNOWN_MODEL
        )
        usage = _extract_usage(response)
        if usage is not None:
            input_tokens, output_tokens = usage
            emit_llm_usage(
                self._agent_name,
                model,
                input_tokens,
                output_tokens,
                api_key=self._api_key,
            )
        self._emit_messages(prompt, run_id, model, response)

    def _emit_messages(
        self,
        prompt: _Prompt | None,
        run_id: UUID,
        model: str,
        response: LLMResult,
    ) -> None:
        """Convert this call's prompt delta and completion and emit them.

        Guarded because a converter works on provider-shaped data: losing a
        transcript row must not fail the run it was logging."""
        if not log_messages_enabled():
            return
        turn_key, messages, _ = prompt or _Prompt(self.turn_key(run_id), [], "")
        # Convert before advancing: a failed conversion must not spend the
        # turn's seq on an event that never goes out.
        try:
            converted = [input_message(message) for message in messages]
            output = output_messages(response)
        except Exception:
            _log.exception("converting LLM messages raised; dropping this event")
            return
        delta = self._cursor.advance(turn_key, converted)
        # First event of each turn only — the system prompt is the head of the
        # list here, so lift it into its own field rather than store it twice.
        instructions, new_input = (
            split_system_instructions(delta.messages)
            if delta.seq == 0
            else ([], delta.messages)
        )
        emit_llm_messages(
            self._agent_name,
            model,
            new_input,
            output,
            turn_key=turn_key,
            message_seq=delta.seq,
            system_instructions=instructions or None,
            resynced=delta.resynced,
            api_key=self._api_key,
        )


def _started_model(metadata: dict[str, Any] | None, kwargs: dict[str, Any]) -> str:
    """The model id LangChain names on the *request* side, or ``""`` — the
    request half always knows what it is calling, while the response echoes a
    model only if the provider bothered. ``invocation_params`` is the older
    shape of the same thing."""
    if metadata and metadata.get("ls_model_name"):
        return str(metadata["ls_model_name"])
    params = kwargs.get("invocation_params") or {}
    return str(params.get("model_name") or params.get("model") or "")


def _first_message(response: LLMResult) -> BaseMessage | None:
    """The ``AIMessage`` of the first generation, or ``None`` — a bare
    ``Generation`` from a non-chat LLM carries none, and neither does an empty
    result."""
    generations = response.generations[0] if response.generations else []
    return getattr(generations[0], "message", None) if generations else None


def _model_name(response: LLMResult) -> str:
    """Read the model name off the response, ``response_metadata`` first
    because ``llm_output`` is ``None`` for a streamed one (confirmed against a
    real ``ChatOpenAI`` call, not assumed) and second for providers that fill
    only the legacy field."""
    message = _first_message(response)
    response_metadata = getattr(message, "response_metadata", None) or {}
    return response_metadata.get("model_name") or (response.llm_output or {}).get(
        "model_name", ""
    )


def _extract_usage(response: LLMResult) -> tuple[int, int] | None:
    """(input_tokens, output_tokens), or None when the provider reported no
    usage at all."""
    usage_metadata = getattr(_first_message(response), "usage_metadata", None)
    if usage_metadata:
        return usage_metadata["input_tokens"], usage_metadata["output_tokens"]
    token_usage = (response.llm_output or {}).get("token_usage")
    if not token_usage:
        return None
    return token_usage.get("prompt_tokens", 0), token_usage.get("completion_tokens", 0)
