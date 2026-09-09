"""Head+tail caps for LLM message content (hexgate.audit).

These are the SDK-side caps for the ``hexgate.messages`` scope. The platform
enricher imports the same functions, so what is asserted here about shape and
size is what the stored row will have."""

from __future__ import annotations

import copy
import json
from typing import Any

from hexgate.audit import (
    MAX_INPUT_MESSAGES_BYTES,
    MAX_OUTPUT_MESSAGES_BYTES,
    MAX_SYSTEM_INSTRUCTIONS_BYTES,
    cap_json_head_tail,
    truncate_head_tail,
)


def _json_size(value: Any) -> int:
    """The platform's measure: serialized-JSON bytes, ``default=str``."""
    return len(json.dumps(value, default=str).encode("utf-8"))


def _message(content: str, role: str = "user") -> dict[str, Any]:
    return {"role": role, "parts": [{"type": "text", "content": content}]}


# --- cap constants ------------------------------------------------------------


def test_caps_are_the_design_values() -> None:
    assert MAX_INPUT_MESSAGES_BYTES == 32 * 1024
    assert MAX_OUTPUT_MESSAGES_BYTES == 8 * 1024
    assert MAX_SYSTEM_INSTRUCTIONS_BYTES == 8 * 1024


# --- truncate_head_tail -------------------------------------------------------


def test_truncate_head_tail_happy_path() -> None:
    text = "H" * 500 + "M" * 500 + "T" * 500
    out = truncate_head_tail(text, max_bytes=200)
    assert len(out.encode("utf-8")) <= 200
    assert out.startswith("HHH")
    assert out.endswith("TTT")
    assert "M" not in out
    kept = len(out.encode("utf-8")) - len(
        out.split("...[truncated ")[1].split(" bytes]...")[0]
    )
    assert "...[truncated " in out and " bytes]... " in out
    assert kept > 0


def test_when_text_fits_then_it_is_returned_unchanged() -> None:
    text = "short"
    assert truncate_head_tail(text, max_bytes=100) is text


def test_when_the_marker_names_omitted_bytes_then_the_count_is_exact() -> None:
    text = "a" * 10_000
    out = truncate_head_tail(text, max_bytes=1_000)
    omitted = int(out.split("...[truncated ")[1].split(" bytes]...")[0])
    kept_content = len(out.encode("utf-8")) - len(
        f" ...[truncated {omitted} bytes]... ".encode("utf-8")
    )
    assert omitted + kept_content == 10_000


def test_when_text_is_multibyte_then_cuts_land_on_code_points() -> None:
    text = "é" * 2_000  # 2 bytes each
    out = truncate_head_tail(text, max_bytes=301)  # odd budget forces a split
    assert len(out.encode("utf-8")) <= 301
    assert out.startswith("é") and out.endswith("é")
    assert "�" not in out


def test_when_the_cap_is_smaller_than_the_marker_then_only_a_head_survives() -> None:
    out = truncate_head_tail("x" * 100, max_bytes=10)
    assert out == "x" * 10


# --- cap_json_head_tail -------------------------------------------------------


def test_cap_json_head_tail_happy_path() -> None:
    messages = [_message("Q" * 1_000 + "C" * 100_000 + "A" * 1_000)]
    out, truncated = cap_json_head_tail(messages, cap=MAX_INPUT_MESSAGES_BYTES)
    assert truncated is True
    assert _json_size(out) <= MAX_INPUT_MESSAGES_BYTES
    # Shape survives: still one user message with one text part.
    assert out[0]["role"] == "user"
    assert out[0]["parts"][0]["type"] == "text"
    content = out[0]["parts"][0]["content"]
    assert content.startswith("QQQ") and content.endswith("AAA")
    assert "...[truncated " in content
    # And it round-trips as JSON — head+tail was applied to the text, not to
    # the serialized document.
    assert json.loads(json.dumps(out)) == out


def test_when_under_cap_then_a_copy_is_returned_untouched() -> None:
    messages = [_message("hello")]
    out, truncated = cap_json_head_tail(messages, cap=1_024)
    assert truncated is False
    assert out == messages
    assert out is not messages
    assert out[0] is not messages[0]


def test_when_several_leaves_then_the_largest_is_cut_first() -> None:
    small = _message("tool result: ok", role="tool")
    big = _message("R" * 50_000, role="user")
    out, truncated = cap_json_head_tail([small, big], cap=8 * 1024)
    assert truncated is True
    assert out[0] == small
    assert "...[truncated " in out[1]["parts"][0]["content"]
    assert _json_size(out) <= 8 * 1024


def test_when_two_leaves_are_both_large_then_both_are_cut_until_it_fits() -> None:
    out, truncated = cap_json_head_tail(
        [_message("A" * 30_000), _message("B" * 30_000)], cap=8 * 1024
    )
    assert truncated is True
    assert _json_size(out) <= 8 * 1024
    assert all("...[truncated " in m["parts"][0]["content"] for m in out)
    # Equal leaves keep equal shares: the overage is not billed to whichever
    # one the walk reached first, so neither collapses to the floor.
    kept = [len(m["parts"][0]["content"]) for m in out]
    assert min(kept) > 1_000
    assert max(kept) - min(kept) <= 1
    # And the allowance is spent, not abandoned.
    assert _json_size(out) > (8 * 1024) // 2


def test_cap_json_head_tail_does_not_mutate_the_input() -> None:
    messages = [_message("X" * 50_000)]
    before = copy.deepcopy(messages)
    cap_json_head_tail(messages, cap=1_024)
    assert messages == before


def test_when_structure_alone_exceeds_the_cap_then_a_preview_wrapper_ships() -> None:
    """Hundreds of tiny messages: no string cut can help, so the existing
    preview wrapper is the fallback — inside a list so the attribute keeps its
    container type."""
    messages = [_message("ok", role="tool") for _ in range(2_000)]
    out, truncated = cap_json_head_tail(messages, cap=1_024)
    assert truncated is True
    assert _json_size(out) <= 1_024
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["_truncated"] is True
    assert out[0]["original_bytes"] > 1_024


def test_when_value_is_a_bare_string_then_it_is_cut_head_tail() -> None:
    out, truncated = cap_json_head_tail("s" * 10_000, cap=200)
    assert truncated is True
    assert isinstance(out, str)
    assert _json_size(out) <= 200
    assert out.startswith("sss") and out.endswith("sss")


def test_when_content_is_non_ascii_then_the_json_measure_still_fits() -> None:
    """``json.dumps`` escapes each CJK character to 6 bytes; the cap is on
    that serialized form, not on the raw text."""
    out, truncated = cap_json_head_tail([_message("日" * 20_000)], cap=4_096)
    assert truncated is True
    assert _json_size(out) <= 4_096
    content = out[0]["parts"][0]["content"]
    assert content.startswith("日") and content.endswith("日")
    # The escape cost is charged once, not twice: at 6 JSON bytes a character
    # a 4 KiB cap buys ~680 of them, so anything near the 64-byte floor means
    # a UTF-8 length was cut by a JSON-measured overage.
    assert len(content) > 500
    assert _json_size(out) > 4_096 // 2


def test_when_content_is_escape_heavy_then_the_allowance_is_still_spent() -> None:
    """A tool result that is itself JSON: every quote costs two JSON bytes."""
    payload = json.dumps({f"key_{i}": f"value_{i}" for i in range(2_000)})
    out, truncated = cap_json_head_tail([_message(payload)], cap=8 * 1024)
    assert truncated is True
    assert _json_size(out) <= 8 * 1024
    assert _json_size(out) > (8 * 1024) // 2


def test_when_a_message_list_holds_tuples_then_they_are_capped_as_lists() -> None:
    """LangChain's ``[("system", ...), ("human", ...)]`` form: tuples are not
    assignable, so a path-based cut would raise on the export path."""
    out, truncated = cap_json_head_tail(
        [("system", "s" * 100), ("human", "h" * 50_000)], cap=4_096
    )
    assert truncated is True
    assert _json_size(out) <= 4_096
    assert out[0] == ["system", "s" * 100]
    assert out[1][0] == "human"
    assert "...[truncated " in out[1][1]


def test_when_the_cap_is_below_the_wrapper_headroom_then_it_still_returns() -> None:
    """``cap_json_head_tail`` is public and the enricher imports it, so a small
    cap must terminate rather than halve a negative preview budget forever."""
    messages = [_message("ok", role="tool") for _ in range(200)]
    for cap in (513, 512, 400, 64):
        out, truncated = cap_json_head_tail(messages, cap=cap)
        assert truncated is True
        assert isinstance(out, list) and len(out) == 1
        assert out[0]["_truncated"] is True
