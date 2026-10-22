import json

from hexgate_mcp.tools import RESULT_CAP_BYTES, TRUNCATED_MARKER, result_text


def test_result_text_happy_path():
    assert json.loads(result_text({"a": [1, 2]})) == {"a": [1, 2]}


def test_when_result_is_exactly_the_cap_then_it_is_sent_whole():
    value = "x" * (RESULT_CAP_BYTES - 2)  # json.dumps adds the two quotes

    assert result_text(value) == json.dumps(value)


def test_when_result_exceeds_the_cap_then_head_is_kept_with_marker():
    value = "x" * RESULT_CAP_BYTES

    text = result_text(value)

    assert len(text.encode()) == RESULT_CAP_BYTES
    assert text.startswith('"xxx')
    assert text.endswith(TRUNCATED_MARKER)


def test_when_cut_falls_inside_a_character_then_it_is_dropped():
    # "é" is two bytes, and the opening quote puts the even cut inside one.
    assert len(TRUNCATED_MARKER) % 2 == 0
    value = "é" * RESULT_CAP_BYTES

    text = result_text(value)

    assert len(text.encode()) == RESULT_CAP_BYTES - 1
    assert text.endswith("é" + TRUNCATED_MARKER)


def test_when_list_exceeds_the_cap_then_whole_items_are_kept():
    item = {"id": "p", "name": "x" * 1000}
    value = [item] * 300  # ~300 KiB

    text = result_text(value)

    assert len(text.encode()) <= RESULT_CAP_BYTES
    result = json.loads(text)
    assert result["truncated"] is True
    assert 0 < len(result["items"]) < len(value)
    assert result["items"] == value[: len(result["items"])]
    # One more item would not have fit.
    one_more = json.dumps(
        {"truncated": True, "items": value[: len(result["items"]) + 1]},
        ensure_ascii=False,
    )
    assert len(one_more.encode()) > RESULT_CAP_BYTES


def test_when_first_list_item_exceeds_the_cap_then_head_is_kept():
    value = ["x" * RESULT_CAP_BYTES]

    text = result_text(value)

    assert len(text.encode()) == RESULT_CAP_BYTES
    assert text.startswith('["xxx')
    assert text.endswith(TRUNCATED_MARKER)
