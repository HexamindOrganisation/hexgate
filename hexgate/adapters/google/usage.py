"""Google ADK per-call token usage and message capture via a ``BasePlugin``.

``after_model_callback`` fires once per underlying model call, so a single
run with several turns (tool-calling loops, sub-agent handoffs) can emit
more than one usage and message event. ``callback_context.agent_name`` is read
per-call rather than fixed at construction, since one ``Runner`` can drive
several named sub-agents.

The two model callbacks come as a pair because neither half carries the whole
call: ``before_model_callback`` sees the prompt (``LlmRequest.contents`` plus
the system instruction), ``after_model_callback`` sees the completion and the
token counts. The request side is stashed under the invocation id and picked
up when the response lands, so usage and messages leave from one call site.

The ``turn_key`` is ``invocation_id`` plus the agent name. ADK rebuilds
``contents`` from the session on every call, so the invocation identifies the
conversation — but not on its own the *list*: a ``ParallelAgent`` runs its
sub-agents concurrently under one invocation id, each seeing its own filtered
``contents``, so keyed by the id alone they would overwrite each other's stash
and interleave in one cursor. The agent name separates them. It also gives a
``transfer_to_agent`` target its own list, which is what it has — ADK rewrites
the source agent's turns into narrated text for it — so the handoff starts at
``message_seq`` 0 with its own instructions rather than resyncing the parent's.
An ``AgentTool`` runs its own nested ``Runner`` and so gets an id of its own.

Turning ``Content``/``Part`` into GenAI messages is ``messages.py``'s job,
beside this one.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, NamedTuple

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from hexgate.adapters.google.messages import (
    input_message,
    output_messages,
    system_parts,
)
from hexgate.tracing.messages import (
    MessageCursor,
    emit_llm_messages,
    log_messages_enabled,
)
from hexgate.tracing.usage import emit_llm_usage

_log = logging.getLogger(__name__)


class _Prompt(NamedTuple):
    """What ``before_model_callback`` saw, held until the matching
    ``after_model_callback``."""

    model: str
    contents: list[types.Content]
    system_instruction: Any


_NO_PROMPT = _Prompt("", [], None)


def _turn_key(callback_context: CallbackContext) -> str:
    """Identity of the *message list* this call extends (see module docstring)."""
    return f"{callback_context.invocation_id}:{callback_context.agent_name}"


class HexgateUsagePlugin(BasePlugin):
    """Emits one :class:`~hexgate.tracing.usage.LlmUsageEvent` and one
    :class:`~hexgate.tracing.messages.LlmMessageEvent` per model call. Never
    rewrites the response — always returns ``None`` so the real model output
    reaches the agent unchanged."""

    # Cap on tracked message lists. ADK skips after_run_callback when the agent
    # loop raises or the caller stops iterating — a policy deny and an SSE
    # client disconnect both do — so this plugin, shared across the runner's
    # life, would otherwise retain state per abnormally-ended run. Every cursor
    # entry has a _pending entry (see _tombstone), so this one cap bounds both.
    _MAX_PENDING = 256

    def __init__(self, *, api_key: str) -> None:
        super().__init__(name="hexgate_usage")
        self._api_key = api_key
        self._cursor = MessageCursor()
        # Request side of each in-flight model call, keyed by invocation: the
        # response callback carries no prompt, so it has to be handed one.
        self._pending: OrderedDict[str, _Prompt] = OrderedDict()

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> None:
        """Stash the prompt for the matching ``after_model_callback``.

        Stashed even with message logging off, because the request is also
        where the model name comes from when the response omits it.
        """
        key = _turn_key(callback_context)
        self._pending[key] = _Prompt(
            llm_request.model or "",
            # Copied: ADK mutates this list while building the request, and the
            # delta must be measured against the prompt actually sent. Skipped
            # when nothing will read it, so an opted-out process holds nothing.
            list(llm_request.contents) if log_messages_enabled() else [],
            llm_request.config.system_instruction,
        )
        self._pending.move_to_end(key)
        while len(self._pending) > self._MAX_PENDING:
            stale, _ = self._pending.popitem(last=False)
            self._cursor.reset(stale)  # its run is gone; drop both halves
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> None:
        key = _turn_key(callback_context)
        # In SSE streaming mode this callback also fires once per chunk; only
        # the aggregated response ends a call, so only it consumes the stash.
        completed = not llm_response.partial
        prompt = self._pending.get(key) or _NO_PROMPT
        if completed:
            self._tombstone(key)
        # ADK builds that aggregate without a model_version
        # (``StreamingResponseAggregator.close``), and both events are rejected
        # platform-side on an empty model, so fall back to the request's.
        model = llm_response.model_version or prompt.model
        usage = llm_response.usage_metadata
        # One emit per stashed request, for both events. A chunk repeats the
        # call's running counts, and on the non-progressive streaming path ADK
        # yields two responses that both read as completed — either would bill
        # one answer twice. A turn that streams no content at all produces no
        # completed response, so it goes unrecorded; ADK offers nowhere
        # reliable to flush it from, since the run-end callback is exactly what
        # an aborted run skips.
        if completed and prompt is not _NO_PROMPT:
            if usage is not None:
                emit_llm_usage(
                    callback_context.agent_name,
                    model,
                    usage.prompt_token_count or 0,
                    usage.candidates_token_count or 0,
                    api_key=self._api_key,
                )
            self._emit_messages(callback_context, key, model, llm_response, prompt)
        return None

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        """Drop every message list of this invocation — one per agent that ran
        — the reset each adapter owes
        :class:`~hexgate.tracing.messages.MessageCursor` on run end."""
        prefix = f"{invocation_context.invocation_id}:"
        for key in [k for k in self._pending if k.startswith(prefix)]:
            del self._pending[key]
            self._cursor.reset(key)
        return None

    def _tombstone(self, key: str) -> None:
        """Release a completed call's prompt but keep its key.

        Dropping the key outright would strand the cursor entry beside it: the
        LRU below only reaches cursors through ``_pending``, and ADK skips the
        run-end callback on an abort. Keeping an empty marker is what makes
        "every cursor entry has a _pending entry" true, so one cap bounds both.
        """
        if key in self._pending:
            self._pending[key] = _NO_PROMPT

    def _emit_messages(
        self,
        callback_context: CallbackContext,
        key: str,
        model: str,
        llm_response: LlmResponse,
        prompt: _Prompt,
    ) -> None:
        """Convert this call's prompt delta and completion and emit them.

        Guarded because ADK's ``PluginManager`` re-raises out of this callback:
        losing a transcript row must not fail the run it was logging.
        """
        if not log_messages_enabled():
            return
        # Convert before advancing: a failed conversion must not spend the
        # turn's seq on an event that never goes out.
        try:
            new_input = [input_message(content) for content in prompt.contents]
            output = output_messages(llm_response.content)
        except Exception:
            _log.exception("converting LLM messages raised; dropping this event")
            return
        delta = self._cursor.advance(key, new_input)
        emit_llm_messages(
            callback_context.agent_name,
            model,
            delta.messages,
            output,
            turn_key=key,
            message_seq=delta.seq,
            # First event of each invocation only — repeating the instructions
            # would spend the 8 KiB budget on every turn.
            system_instructions=(
                system_parts(prompt.system_instruction) if delta.seq == 0 else None
            ),
            resynced=delta.resynced,
            api_key=self._api_key,
        )
