"""Tests for inline terminal app rendering helpers."""

from __future__ import annotations

from coolagents.cli.app import _tail_text


def test_tail_text_keeps_last_lines_of_long_output() -> None:
    """Keep only the trailing lines for live rendering."""
    text = "\n".join(f"line {index}" for index in range(1, 21))

    tailed, truncated = _tail_text(text, max_lines=4, max_chars=10_000)

    assert tailed == "line 17\nline 18\nline 19\nline 20"
    assert truncated is True


def test_tail_text_caps_large_character_payloads() -> None:
    """Trim very large text blocks before line tailing."""
    text = "a" * 50 + "tail"

    tailed, truncated = _tail_text(text, max_lines=5, max_chars=8)

    assert tailed == "aaaatail"
    assert truncated is True


def test_tail_text_reports_when_text_is_not_truncated() -> None:
    """Leave short text untouched and mark it as fully visible."""
    tailed, truncated = _tail_text("short answer", max_lines=5, max_chars=100)

    assert tailed == "short answer"
    assert truncated is False
