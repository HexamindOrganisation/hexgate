"""LangChain per-call token usage and message capture via a ``BaseCallbackHandler``.

LangGraph propagates ``config["callbacks"]`` down through every node, so a
handler placed there (see ``HexgateLangchainAgent._with_callbacks``) has its
callbacks invoked by the framework itself on each underlying chat-model call —
not something this module drives directly. A single ``.invoke()`` can therefore
emit more than one usage and message event, once per LLM turn in the run.

The callbacks come as a pair because neither half carries the whole call:
``on_chat_model_start`` sees the prompt (the entire message list the model is
about to receive), ``on_llm_end`` sees the completion and the token counts.
The request side is stashed under LangChain's own ``run_id`` — which names one
model call, start and end — and picked up when the response lands, so usage and
messages leave from one call site.

What reaches the wire is the *delta* — only the messages added since this turn's
last call — because the list is the whole conversation so far and re-sending it
every call would store the transcript once per turn. Working out that delta is
:class:`~hexgate.tracing.messages.MessageCursor`'s job; turning ``BaseMessage``
objects into GenAI messages is ``messages.py``'s, beside this one.
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

# Last resort, when neither the response nor the request named a model. The
# platform rejects an empty model outright (``min_length=1``), which would drop
# the whole event rather than just its least interesting field. Spelled as the
# OpenAI adapter spells its own unresolved model — but it means less here, so
# ``_started_model`` is tried first: an OpenAI "default" is one agent that
# declared no model, whereas a LangChain one would be several known models
# collapsed into a single bucket on the usage breakdown.
_UNKNOWN_MODEL = "default"


class _Prompt(NamedTuple):
    """What a start callback saw, held until the matching ``on_llm_end``.

    ``messages`` is the whole conversation the model is about to see, not a
    delta; working out what is new is the cursor's job. ``turn_key`` is carried
    along so :meth:`HexgateUsageCallbackHandler.end_run` can drop the stashes of
    one finished run without touching a concurrent one's, and ``model`` because
    the request side is the only half that always knows it (see
    :func:`_started_model`).
    """

    turn_key: str
    messages: list[Any]
    model: str


class HexgateUsageCallbackHandler(BaseCallbackHandler):
    """Emits one :class:`~hexgate.tracing.usage.LlmUsageEvent` and one
    :class:`~hexgate.tracing.messages.LlmMessageEvent` per ``on_llm_end``
    callback, from the prompt stashed by the matching start callback.

    Reads the standardized ``UsageMetadata`` off the response message when
    the provider populates it, falling back to ``llm_output["token_usage"]``
    for providers that only fill the legacy field. Does nothing when
    neither is present — a provider that reports no usage must not
    synthesize a zeroed event. Message capture is independent of that: a
    transcript is worth keeping whether or not the provider counted tokens.

    One instance is built per :class:`~hexgate.adapters.langchain.agent.HexgateLangchainAgent`
    and serves every call that proxy makes, concurrent ones included — unlike
    the OpenAI adapter's per-run hooks. That is why the cursor is keyed by run
    and why :meth:`end_run` exists: without it the per-``turn_key`` state of
    every run the process ever made would accumulate for its lifetime.
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

        The Hexgate run id, not LangChain's ``run_id`` or ``parent_run_id``:
        both name one model call — they are UUIDv7s minted per callback, so a
        node's second turn gets a different pair from its first — and a fresh
        key every turn would restart ``message_seq`` at 0 and re-send the
        entire conversation as a snapshot each time, which is precisely what
        the delta exists to avoid. The run id is the coarsest identity that
        survives a whole ``invoke``/``astream``.

        It is coarser than one message list, and knowingly so: a run that calls
        a second model with its own list — a summariser node, a router, an
        unwrapped sub-graph reached through a tool — files those calls under
        the same key, and the two lists then fail each other's prefix check and
        go out as resyncs. The rows stay complete and flagged (``resynced`` is
        exactly "the list was restated"); what is lost is the delta's
        compression and per-list gap detection for that run. Nothing on the
        LangChain callback surface identifies a message list — ``checkpoint_ns``
        and ``langgraph_step`` change per node execution too — so the
        alternative is a key that changes every turn, which is worse. A
        sub-agent wrapped by its own ``HexgateLangchainAgent`` is unaffected:
        its ``run_scope`` mints a new run id.

        Outside a run scope — a handler attached by hand to a raw graph, rather
        than through ``HexgateLangchainAgent``, which always opens one — there
        is no such identity to borrow, so the call falls back to being its own
        one-message list. That logs a full snapshot per call, which is verbose;
        a shared per-instance fallback would instead file unrelated
        conversations under one key, which is wrong.
        """
        run = get_run_facts().id
        return f"{run or f'lc-{run_id}'}:{self._agent_name}"

    def end_run(self, turn_key: str) -> None:
        """Forget one finished run's cursor state and any stash it left behind.

        The key is passed in, never re-read from the run scope on the way out.
        ``stream``/``astream`` put this call inside an async generator, and a
        consumer that breaks out early leaves that generator to be finalized
        later — in a *different* ``Context``, which is why
        ``runtime.run_facts`` restores by ``set()`` rather than a token reset.
        A write there is merely awkward; a read is silently wrong. Re-deriving
        the key at exit returns whichever run happens to be bound at
        finalization time, so the abandoned run's state would survive while a
        live one's was swept out from under it: its cursor reset mid-run (the
        whole history re-sent at ``seq`` 0, unflagged, past the gap detection a
        reader relies on) and its in-flight prompt dropped.

        The stash sweep is by ``turn_key`` rather than a wholesale clear: one
        handler serves concurrent runs, and a cancelled call leaves an entry
        that neither ``on_llm_end`` nor ``on_llm_error`` will ever collect.

        One key, so one message list. An agent invoked from inside another
        one's graph opens its own run scope, and LangChain hands this handler
        that nested agent's calls too — under the nested run's key, which this
        sweep does not name and the nested agent's own handler does not hold.
        That entry outlives the run: ~200 bytes per nested run, measured.
        Closing it needs the cursor's eviction contract to change rather than
        this method, so it is left as a known cost — see the same shape on
        ``HexgateLangchainAgent`` and ``HexgateAgent``.
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
        """Stash the prompt for the matching ``on_llm_end``.

        ``messages`` is one list per prompt and a chat-model call has exactly
        one, so ``messages[0]`` is the conversation. It is the whole list, not
        a delta.

        Stashed whether or not message logging is on, because the *usage*
        event reads the model off this too and that stream has nothing to do
        with ``HEXGATE_LOG_MESSAGES``. Only the conversation itself is gated:
        an opted-out process must not pay to copy and convert a prompt it will
        not keep, but a model id it already has in hand costs nothing.
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
        """The same stash for a non-chat LLM, whose prompts are bare strings.

        Chat models never reach here — LangChain routes them to
        ``on_chat_model_start`` above and only falls back to this one when that
        override is missing. Gated the same way, for the same reason.
        """
        self._pending[run_id] = _Prompt(
            self.turn_key(run_id),
            list(prompts) if log_messages_enabled() else [],
            _started_model(metadata, kwargs),
        )

    async def on_llm_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        """Drop the stash of a call that failed, since no ``on_llm_end`` will.

        Nothing is emitted: there is no completion to record, and the cursor is
        deliberately left untouched so the next call still measures its delta
        against the last prompt that actually reached a model.
        """
        self._pending.pop(run_id, None)

    # --- Response side ------------------------------------------------------

    async def on_llm_end(
        self, response: LLMResult, *, run_id: UUID, **kwargs: Any
    ) -> None:
        # Async so LangChain's AsyncCallbackManager awaits this inline on the
        # real event loop (asyncio.iscoroutinefunction check in
        # _ahandle_event_for_handler) instead of dispatching it to a thread
        # pool executor — a plain sync def here runs off-loop, and
        # emit_llm_usage's sender never gets a valid loop to schedule its
        # HTTP send on, silently dropping every event.
        # Popped once here, not inside _emit_messages: the model on it is the
        # usage event's fallback too, and an entry left behind when message
        # logging is off would never be collected.
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
        transcript row must not fail the run it was logging.
        """
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
    """The model id LangChain names on the *request* side, or ``""``.

    The request half always knows which model it is about to call, while the
    response only carries one if the provider echoed it — so this is what keeps
    :data:`_UNKNOWN_MODEL` a genuine last resort rather than a bucket that
    collapses several known models into one on the usage breakdown.
    ``ls_model_name`` is the standard metadata key every chat model stamps;
    ``invocation_params`` is the older shape.
    """
    if metadata and metadata.get("ls_model_name"):
        return str(metadata["ls_model_name"])
    params = kwargs.get("invocation_params") or {}
    return str(params.get("model_name") or params.get("model") or "")


def _first_message(response: LLMResult) -> Any:
    """The ``AIMessage`` of the first generation, or ``None``.

    A ``ChatGeneration`` carries one; the bare ``Generation`` a non-chat LLM
    yields does not, and neither does an empty result — both of which the
    message path now reaches, since ``on_llm_start`` stashes plain prompts too.
    """
    generations = response.generations[0] if response.generations else []
    return getattr(generations[0], "message", None) if generations else None


def _model_name(response: LLMResult) -> str:
    """Read the model name off the response.

    ``llm_output`` is ``None`` for a streamed response (LangChain's default
    ``_combine_llm_outputs`` — and even providers that override it, like
    ChatOpenAI, only combine per-chunk outputs that are non-``None``, which
    streaming chunks typically aren't) — confirmed empirically against a
    real streaming ``ChatOpenAI`` call, not assumed. ``response_metadata``
    on the message is populated in both the streaming and non-streaming
    case, so it's the primary source; ``llm_output`` stays as a fallback
    for providers that only populate the legacy field.
    """
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
