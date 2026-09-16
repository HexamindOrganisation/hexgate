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

``invocation_id`` is the ``turn_key``: ADK rebuilds ``contents`` from the
session on every call, so one invocation is one list, and an ``AgentTool``
runs its own nested ``Runner`` and so gets an id of its own. A
``transfer_to_agent`` handoff stays on that list but ADK rewrites the other
agent's turns into narrated text for the target, so the cursor sees a
rewritten history and resyncs once per hop — the documented degradation.

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


class HexgateUsagePlugin(BasePlugin):
    """Emits one :class:`~hexgate.tracing.usage.LlmUsageEvent` and one
    :class:`~hexgate.tracing.messages.LlmMessageEvent` per model call. Never
    rewrites the response — always returns ``None`` so the real model output
    reaches the agent unchanged."""

    # Cap on stashed prompts. ADK skips after_run_callback when the agent loop
    # raises or the caller stops iterating — a policy deny and an SSE client
    # disconnect both do — so this plugin, shared across the runner's life,
    # would otherwise retain one whole conversation per abnormally-ended run.
    # Same bound _HexgateReachPlugin keeps, sized for in-flight calls rather
    # than invocations because an entry here is the prompt, not a counter.
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
        key = callback_context.invocation_id
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
        key = callback_context.invocation_id
        # In SSE streaming mode this callback also fires once per chunk; only
        # the aggregated response ends a call, so only it consumes the stash.
        completed = not llm_response.partial
        prompt = (
            self._pending.pop(key, None) if completed else self._pending.get(key)
        ) or _NO_PROMPT
        # ADK builds that aggregate without a model_version
        # (``StreamingResponseAggregator.close``), and both events are rejected
        # platform-side on an empty model, so fall back to the request's.
        model = llm_response.model_version or prompt.model
        usage = llm_response.usage_metadata
        # Chunks repeat the call's running counts, so counting them too would
        # bill one answer once per chunk.
        if completed and usage is not None:
            emit_llm_usage(
                callback_context.agent_name,
                model,
                usage.prompt_token_count or 0,
                usage.candidates_token_count or 0,
                api_key=self._api_key,
            )
        if completed and prompt is not _NO_PROMPT:
            self._emit_messages(callback_context, model, llm_response, prompt)
        return None

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        """Drop this invocation's cursor and stash, the reset every adapter
        owes :class:`~hexgate.tracing.messages.MessageCursor` on run end."""
        self._cursor.reset(invocation_context.invocation_id)
        self._pending.pop(invocation_context.invocation_id, None)
        return None

    def _emit_messages(
        self,
        callback_context: CallbackContext,
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
        key = callback_context.invocation_id
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
