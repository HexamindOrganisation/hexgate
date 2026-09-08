"""MessageCursor — turning each hook's whole input list into the delta new to
that call, per ``hexgate.tracing.semconv``'s one-event-per-LLM-call rule.

The cases are the ones the frameworks actually produce: a plain append, the
several messages a parallel tool call adds at once, a list the framework
trimmed to fit a context window, a list it summarised in place, and two lists
of one session interleaved.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from hexgate.tracing.messages import MessageCursor, MessageDelta


def _msg(content: str, role: str = "user") -> dict[str, Any]:
    return {"role": role, "parts": [{"type": "text", "content": content}]}


def _tool_result(call_id: str, content: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "parts": [{"type": "tool_call_response", "id": call_id, "response": content}],
    }


def test_advance_happy_path() -> None:
    """A first call is entirely new, and the call after it contributes only the
    messages appended since."""
    cursor = MessageCursor()

    first = cursor.advance("run-1", [_msg("What is the weather in Paris?")])
    assert first == MessageDelta(
        messages=[_msg("What is the weather in Paris?")], seq=0, resynced=False
    )

    second = cursor.advance(
        "run-1",
        [
            _msg("What is the weather in Paris?"),
            _msg("Let me check.", role="assistant"),
            _msg("And in Lyon?"),
        ],
    )
    assert second == MessageDelta(
        messages=[_msg("Let me check.", role="assistant"), _msg("And in Lyon?")],
        seq=1,
        resynced=False,
    )


def test_when_several_messages_appended_then_all_are_new() -> None:
    """Two parallel tool calls grow the list by three — the assistant message
    holding both calls plus one result each — and every one of them is emitted.
    Tool results in particular: a decision event records the call but never its
    return value, so this is the only place that value is stored."""
    cursor = MessageCursor()
    history = [_msg("Weather in Paris and Lyon?")]
    cursor.advance("run-1", history)

    assistant = _msg("calling get_weather twice", role="assistant")
    history = [
        *history,
        assistant,
        _tool_result("c1", "24C"),
        _tool_result("c2", "22C"),
    ]

    delta = cursor.advance("run-1", history)
    assert delta.messages == [
        assistant,
        _tool_result("c1", "24C"),
        _tool_result("c2", "22C"),
    ]
    assert delta.resynced is False
    assert delta.seq == 1


def test_when_list_is_trimmed_then_resynced() -> None:
    """The framework drops old turns to fit the context window, so the mark
    points past the end. Slicing there would emit nothing at all; instead the
    whole list is restated and the row is flagged."""
    cursor = MessageCursor()
    history = [_msg(f"turn {i}") for i in range(6)]
    cursor.advance("run-1", history)

    trimmed = history[4:]
    delta = cursor.advance("run-1", trimmed)

    assert delta.messages == trimmed
    assert delta.resynced is True
    assert delta.seq == 1


def test_when_list_is_summarized_in_place_then_resynced() -> None:
    """The hard case: the list keeps its length while holding different
    messages, because the front was replaced by a summary. Slicing at the mark
    would return the right *number* of messages and the wrong ones."""
    cursor = MessageCursor()
    history = [_msg(f"turn {i}") for i in range(4)]
    cursor.advance("run-1", history)

    summarized = [_msg("summary of turns 0-2", role="system"), *history[1:]]
    assert len(summarized) == len(history)

    delta = cursor.advance("run-1", summarized)
    assert delta.messages == summarized
    assert delta.resynced is True


def test_when_two_turn_keys_interleave_then_state_is_per_list() -> None:
    """A handoff or sub-agent shares the session but keeps its own message
    list. Keyed per list, each one's deltas and seq counter are its own; keyed
    per session, the sub-agent's first call would look like a jump and resync
    forever."""
    cursor = MessageCursor()
    main = [_msg("book a flight")]
    sub = [_msg("find flights to Lyon")]

    assert cursor.advance("main", main).messages == main
    assert cursor.advance("sub", sub).messages == sub

    main = [*main, _msg("handing off", role="assistant")]
    sub = [*sub, _msg("AF1234", role="assistant")]

    main_delta = cursor.advance("main", main)
    sub_delta = cursor.advance("sub", sub)

    assert main_delta.messages == [_msg("handing off", role="assistant")]
    assert sub_delta.messages == [_msg("AF1234", role="assistant")]
    assert main_delta.seq == sub_delta.seq == 1
    assert main_delta.resynced is sub_delta.resynced is False


def test_when_nothing_changed_then_delta_is_empty() -> None:
    """An unchanged input list is a real, empty delta — the completion is still
    new — and it does not count as a resync."""
    cursor = MessageCursor()
    history = [_msg("hello")]
    cursor.advance("run-1", history)

    delta = cursor.advance("run-1", list(history))
    assert delta == MessageDelta(messages=[], seq=1, resynced=False)


def test_when_resynced_then_seq_keeps_counting() -> None:
    """A resync restates the history but does not restart the turn: seq stays
    monotonic, so ``seq == 0`` keeps meaning "first event of this turn_key" —
    the adapter's cue to attach system_instructions."""
    cursor = MessageCursor()
    cursor.advance("run-1", [_msg("a")])
    cursor.advance("run-1", [_msg("a"), _msg("b")])

    delta = cursor.advance("run-1", [_msg("z")])
    assert delta.resynced is True
    assert delta.seq == 2
    assert cursor.advance("run-1", [_msg("z"), _msg("y")]).seq == 3


def test_when_key_order_differs_then_still_an_extension() -> None:
    """The prefix is fingerprinted on canonical JSON, so a message dict rebuilt
    in another key order is the same message and does not force a resync."""
    cursor = MessageCursor()
    cursor.advance("run-1", [{"role": "user", "content": "hi"}])

    delta = cursor.advance(
        "run-1", [{"content": "hi", "role": "user"}, _msg("ok", role="assistant")]
    )
    assert delta.resynced is False
    assert delta.messages == [_msg("ok", role="assistant")]


def test_when_a_message_cannot_be_fingerprinted_then_it_resyncs_instead_of_raising() -> (
    None
):
    """``advance`` runs from a framework hook that re-raises into the agent
    run, so it must not raise. A tool result keyed by dates defeats
    ``json.dumps`` — ``default=`` is consulted for values, never for keys — and
    the call degrades to a resync rather than killing the run."""
    cursor = MessageCursor()
    cursor.advance("run-1", [_msg("what did we sell?")])

    unserializable = {"role": "tool", "parts": [{date(2026, 9, 11): 12}]}
    delta = cursor.advance("run-1", [_msg("what did we sell?"), unserializable])

    assert delta.resynced is True
    assert delta.messages == [_msg("what did we sell?"), unserializable]
    assert delta.seq == 1


def test_when_fingerprinting_failed_then_the_next_call_resyncs_too() -> None:
    """A failed call leaves no usable mark, so the call after it restates the
    list as well — rather than slicing against a mark that was never
    established and silently dropping the messages in between."""
    cursor = MessageCursor()
    bad = {"role": "tool", "parts": [{date(2026, 9, 11): 12}]}
    cursor.advance("run-1", [_msg("a"), bad])

    delta = cursor.advance("run-1", [_msg("a"), _msg("b", role="assistant")])
    assert delta.resynced is True
    assert delta.messages == [_msg("a"), _msg("b", role="assistant")]

    # Once a mark is back, extension resumes.
    assert cursor.advance(
        "run-1", [_msg("a"), _msg("b", role="assistant"), _msg("c")]
    ) == MessageDelta(messages=[_msg("c")], seq=2, resynced=False)


def test_when_messages_are_not_json_serializable_then_still_tracked() -> None:
    """Adapters may hand over framework objects they did not flatten.
    ``default=str`` carries them as long as their ``__str__`` is by value — a
    type left on ``object.__repr__`` stringifies to its address and would
    resync on every call, which is why adapters pass dicts or pydantic
    models."""

    class _Opaque:
        def __init__(self, text: str) -> None:
            self.text = text

        def __str__(self) -> str:
            return f"opaque:{self.text}"

    cursor = MessageCursor()
    cursor.advance("run-1", [_Opaque("a")])

    delta = cursor.advance("run-1", [_Opaque("a"), _Opaque("b")])
    assert delta.resynced is False
    assert [str(m) for m in delta.messages] == ["opaque:b"]


def test_reset_happy_path() -> None:
    """Run end forgets the list, so a reused turn_key starts a fresh turn at
    seq 0 rather than inheriting the previous run's mark."""
    cursor = MessageCursor()
    cursor.advance("run-1", [_msg("a"), _msg("b")])
    cursor.reset("run-1")

    delta = cursor.advance("run-1", [_msg("a"), _msg("b")])
    assert delta == MessageDelta(messages=[_msg("a"), _msg("b")], seq=0, resynced=False)


def test_when_turn_key_unknown_then_reset_is_a_noop() -> None:
    """A run that never emitted still ends; the adapter should not have to
    remember whether it did."""
    MessageCursor().reset("never-seen")


def test_clear_happy_path() -> None:
    cursor = MessageCursor()
    cursor.advance("a", [_msg("x")])
    cursor.advance("b", [_msg("y")])
    cursor.clear()

    assert cursor.advance("a", [_msg("x")]).seq == 0
    assert cursor.advance("b", [_msg("y")]).seq == 0
