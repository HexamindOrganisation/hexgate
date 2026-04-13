"""Tests for demo helpers."""

from __future__ import annotations

from asianf.demo import _extract_text


def test_extract_text_handles_plain_string() -> None:
    """Return plain string content unchanged."""
    assert _extract_text("hello") == "hello"


def test_extract_text_handles_mixed_blocks() -> None:
    """Extract text from supported streamed content block shapes."""
    content = [
        {"type": "text", "text": "Hello"},
        {"type": "text_delta", "text": ", world"},
        {"type": "output_text", "text": "!"},
        {"type": "content_block_delta", "delta": {"text": " More."}},
        {"type": "ignored", "text": "nope"},
    ]

    assert _extract_text(content) == "Hello, world! More."


def test_extract_text_ignores_unknown_shapes() -> None:
    """Ignore unsupported streamed content blocks."""
    assert _extract_text([123, {"type": "ignored"}, {"delta": {"text": "missing"}}]) == ""
