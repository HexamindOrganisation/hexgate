import json

from pydantic_ai.messages import (
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ToolCallPart,
    ToolCallPartDelta,
    ToolReturnPart,
)

from hexgate_assistant.events import (
    BROWSER_RESULT_BYTES,
    PING,
    EventTranslator,
    run_error,
    sse,
)


def translate(*native):
    translator = EventTranslator()
    return [e for event in native for e in translator.translate(event)]


def test_translate_happy_path():
    out = translate(
        PartStartEvent(index=0, part=TextPart("He")),
        PartDeltaEvent(index=0, delta=TextPartDelta("llo")),
        PartEndEvent(index=0, part=TextPart("Hello")),
        PartStartEvent(index=1, part=ToolCallPart("policy_graph", '{"a"', "c1")),
        PartDeltaEvent(index=1, delta=ToolCallPartDelta(args_delta=": 1}")),
        PartEndEvent(index=1, part=ToolCallPart("policy_graph", '{"a": 1}', "c1")),
    )

    message_id = out[0]["messageId"]
    assert out == [
        {"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": "assistant"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": "He"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": "llo"},
        {"type": "TEXT_MESSAGE_END", "messageId": message_id},
        {"type": "TOOL_CALL_START", "toolCallId": "c1", "toolCallName": "policy_graph"},
        {"type": "TOOL_CALL_ARGS", "toolCallId": "c1", "delta": '{"a"'},
        {"type": "TOOL_CALL_ARGS", "toolCallId": "c1", "delta": ": 1}"},
        {"type": "TOOL_CALL_END", "toolCallId": "c1"},
    ]


def test_when_two_text_parts_follow_each_other_then_each_is_its_own_message():
    out = translate(
        PartStartEvent(index=0, part=TextPart("a")),
        PartEndEvent(index=0, part=TextPart("a")),
        PartStartEvent(index=2, part=TextPart("b")),
        PartEndEvent(index=2, part=TextPart("b")),
    )

    starts = [e["messageId"] for e in out if e["type"] == "TEXT_MESSAGE_START"]
    assert len(set(starts)) == 2


def test_when_args_arrive_as_a_dict_then_they_stream_as_json():
    out = translate(PartStartEvent(index=0, part=ToolCallPart("t", {"a": 1}, "c1")))

    assert json.loads(out[1]["delta"]) == {"a": 1}


def test_when_a_part_is_thinking_then_nothing_is_streamed():
    assert (
        translate(
            PartStartEvent(index=0, part=ThinkingPart("hmm")),
            PartEndEvent(index=0, part=ThinkingPart("hmm")),
        )
        == []
    )


def test_when_a_tool_result_is_long_then_the_browser_gets_32_kib_with_both_ends():
    content = "HEAD" + "é" * 40_000 + "TAIL"
    event = FunctionToolResultEvent(part=ToolReturnPart("t", content, "c1"))

    (out,) = translate(event)

    assert out["type"] == "TOOL_CALL_RESULT"
    assert out["toolCallId"] == "c1"
    assert len(out["content"].encode()) <= BROWSER_RESULT_BYTES
    assert out["content"].startswith("HEAD") and out["content"].endswith("TAIL")


def test_sse_happy_path():
    assert sse(run_error("timeout", "late")) == (
        'data: {"type":"RUN_ERROR","message":"late","code":"timeout"}\n\n'
    )
    assert PING == ": ping\n\n"


def test_when_streamed_args_arrive_as_a_dict_then_they_go_out_as_json():
    out = translate(
        PartStartEvent(index=0, part=ToolCallPart("t", "", "c1")),
        PartDeltaEvent(index=0, delta=ToolCallPartDelta(args_delta={"a": 1})),
    )

    assert json.loads(out[-1]["delta"]) == {"a": 1}
