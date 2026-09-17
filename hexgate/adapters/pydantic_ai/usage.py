"""Pydantic AI has no per-call usage hook — usage is read from the run
result after the call completes and reported as one aggregate event per
agent run (not per LLM call), a documented limitation vs. the other three
adapters. Message capture rides the same limitation: one event per run,
built from the result's message history.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from pydantic_ai import Agent

from hexgate.adapters.pydantic_ai.messages import run_messages
from hexgate.manifest.pydantic_ai import extract_model
from hexgate.runtime.run_facts import get_run_facts
from hexgate.tracing.messages import emit_llm_messages, log_messages_enabled
from hexgate.tracing.usage import emit_llm_usage

_log = logging.getLogger(__name__)


def _resolve_model(agent: Agent, result: Any) -> str:
    """The model id to report. The actual run's response wins over the agent's
    static config, since pydantic_ai supports per-call model overrides.

    ``AgentRun`` (from ``iter``) has no ``response`` of its own — only the
    ``AgentRunResult`` it finishes with does, so reading one attribute would
    report the configured model for every ``iter`` run that overrode it.
    """
    response = getattr(result, "response", None) or getattr(
        getattr(result, "result", None), "response", None
    )
    return getattr(response, "model_name", None) or extract_model(agent.model) or ""


def emit_run_usage(agent_name: str, agent: Agent, result: Any, *, api_key: str) -> None:
    """Emit one aggregate LlmUsageEvent for a completed pydantic_ai run.

    ``result`` is anything exposing usage and ``.response`` —
    ``AgentRunResult``, ``StreamedRunResult``, and ``AgentRun`` (from
    ``run``/``run_sync``, ``run_stream``, and ``iter`` respectively) all
    qualify.

    ``usage`` is a callable method in pydantic_ai 1.x but a property in 2.x
    (returning ``RunUsage``); resolve both so a 2.x run doesn't raise
    ``TypeError: 'RunUsage' object is not callable``.
    """
    usage_attr = result.usage
    usage = usage_attr() if callable(usage_attr) else usage_attr
    emit_llm_usage(
        agent_name,
        _resolve_model(agent, result),
        usage.input_tokens,
        usage.output_tokens,
        api_key=api_key,
    )


def _session(result: Any) -> list[Any] | None:
    """The whole conversation behind this run, or ``None`` if it cannot be
    read. Only its system prompt is used, so a failure here is not worth
    losing the row over."""
    try:
        return list(result.all_messages())
    except Exception:
        return None


def emit_run_messages(
    agent_name: str,
    agent: Agent,
    result: Any,
    *,
    api_key: str,
    completed: bool = True,
) -> None:
    """Emit one LlmMessageEvent for a pydantic_ai run.

    One event at ``message_seq=0``: with no per-call hook there is no delta
    within a run, so no cursor either. ``completed`` says the run produced an
    answer; without it nothing is hoisted into the completion (see
    ``run_messages``).

    ``new_messages()``, not ``all_messages()``: the latter replays everything a
    caller passed as ``message_history``, so an N-turn chat would store turn 1
    N times, and the newest exchange — the one being investigated — is what the
    256 KiB cap then cuts. Each run's row holds what that run added, and a
    session's rows concatenate back to the conversation.

    ``all_messages()`` still rides along as ``session``, read for its system
    prompt alone: a ``system_prompt=`` lands only in the conversation's first
    request, which from turn two onward is not one of this run's messages.

    Guarded because the run methods do not: losing a transcript row must not
    fail the run it was logging.
    """
    if not log_messages_enabled():
        return
    try:
        input_messages, output, system = run_messages(
            result.new_messages(),
            completed=completed,
            session=_session(result),
        )
    except Exception:
        _log.exception("converting pydantic_ai messages raised; dropping this event")
        return
    # A stream aborted before the first token produced nothing to record.
    if not (input_messages or output or system):
        return
    emit_llm_messages(
        agent_name,
        _resolve_model(agent, result),
        input_messages,
        output,
        # One event per run, so the run is the message list. The fallback keeps
        # two runs outside a run scope from sharing a key at seq 0, which a
        # reader would take for a duplicated row.
        turn_key=get_run_facts().id or uuid4().hex,
        message_seq=0,
        system_instructions=system,
        api_key=api_key,
    )
