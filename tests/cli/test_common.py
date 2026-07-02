"""Tests for shared CLI runtime helpers."""

from __future__ import annotations

import pytest

from hexgate.cli._common import _warn_missing_tool_keys
from hexgate.tools import fetch, web_search


def test_warns_for_each_unset_tool_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LINKUP_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    _warn_missing_tool_keys([web_search, fetch])

    err = capsys.readouterr().err
    assert "LINKUP_API_KEY" in err
    assert "TAVILY_API_KEY" in err


def test_silent_when_keys_present(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LINKUP_API_KEY", "x")
    monkeypatch.setenv("TAVILY_API_KEY", "y")

    _warn_missing_tool_keys([web_search, fetch])

    assert capsys.readouterr().err == ""


def test_warns_only_for_the_missing_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LINKUP_API_KEY", "x")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    _warn_missing_tool_keys([web_search, fetch])

    err = capsys.readouterr().err
    assert "TAVILY_API_KEY" in err
    assert "LINKUP_API_KEY" not in err
