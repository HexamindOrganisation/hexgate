"""agent_usage / user_usage path grammar: <metric>_<N><m|h|d>, whole minutes, 1m to 30d.

A pattern rather than the SDK's closed list, so a new SDK window needs no
platform release. The SDK's list must stay a subset (tests/features/usage).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class UsageMetric(StrEnum):
    INVOCATIONS = "invocations"
    TOOL_CALLS = "tool_calls"
    DENIALS = "denials"
    LLM_CALLS = "llm_calls"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    TOTAL_TOKENS = "total_tokens"


_SECONDS_PER_UNIT: Final = {"m": 60, "h": 3_600, "d": 86_400}
MIN_WINDOW_SECONDS: Final = 60
# usage_minute keeps 35 days (TTL); a longer window would silently read short.
MAX_WINDOW_SECONDS: Final = 30 * 86_400
MAX_PATHS_PER_REQUEST: Final = 64  # the SDK can ask for at most 35
PATH_SEPARATOR: Final = ","
_NO_PATHS: Final = "no paths requested"

_WINDOW_PATTERN: Final = r"(?P<count>[1-9][0-9]{0,6})(?P<unit>[mhd])"
_WINDOW_RE: Final = re.compile(_WINDOW_PATTERN)
_PATH_RE: Final = re.compile(
    rf"(?P<metric>{'|'.join(m.value for m in UsageMetric)})_{_WINDOW_PATTERN}"
)
_WINDOW_GRAMMAR: Final = "<N><m|h|d> between 1m and 30d"
USAGE_PATH_GRAMMAR: Final = (
    f"<metric>_{_WINDOW_GRAMMAR}; metrics: {', '.join(m.value for m in UsageMetric)}"
)


class InvalidUsagePaths(ValueError):
    """The requested paths are empty, too long, or has a bad path. -> 422."""


@dataclass(frozen=True, slots=True, order=True)
class UsageWindowSpec:
    metric: UsageMetric
    window_seconds: int


def parse_usage_path(path: str) -> UsageWindowSpec:
    """Every unit is a whole number of minutes, so whole minutes hold by construction."""
    match = _PATH_RE.fullmatch(path)
    if match is None:
        raise _invalid_path(path)
    seconds = _seconds(match)
    if not MIN_WINDOW_SECONDS <= seconds <= MAX_WINDOW_SECONDS:
        raise _invalid_path(path)
    return UsageWindowSpec(UsageMetric(match["metric"]), seconds)


def parse_window_seconds(window: str) -> int:
    """``7d`` -> 604800, under the same grammar and bounds as a path's window."""
    match = _WINDOW_RE.fullmatch(window)
    if match is None or not MIN_WINDOW_SECONDS <= _seconds(match) <= MAX_WINDOW_SECONDS:
        raise ValueError(f"{window!r}: expected {_WINDOW_GRAMMAR}")
    return _seconds(match)


def _seconds(match: re.Match[str]) -> int:
    return int(match["count"]) * _SECONDS_PER_UNIT[match["unit"]]


def parse_usage_paths(raw: str) -> dict[str, UsageWindowSpec]:
    """The agent route's comma-separated query parameter."""
    return parse_usage_path_list(raw.split(PATH_SEPARATOR))


def parse_usage_path_list(items: Sequence[str]) -> dict[str, UsageWindowSpec]:
    """Keyed by the names as requested: ``invocations_60m`` and ``invocations_1h``
    are two keys with equal specs, because the caller drops keys it didn't ask for."""
    if not items:
        raise InvalidUsagePaths(_NO_PATHS)
    if len(items) > MAX_PATHS_PER_REQUEST:
        raise InvalidUsagePaths(
            f"{len(items)} paths requested; at most {MAX_PATHS_PER_REQUEST} allowed"
        )
    return {item: parse_usage_path(item) for item in items}


def _invalid_path(path: str) -> InvalidUsagePaths:
    return InvalidUsagePaths(f"{path!r}: expected {USAGE_PATH_GRAMMAR}")
