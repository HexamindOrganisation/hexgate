"""agent_usage path grammar: <metric>_<N><m|h|d>, whole minutes, 1m to 30d.

A pattern rather than the SDK's closed list, so a new SDK window needs no
platform release. The SDK's list must stay a subset (tests/features/usage).
"""

from __future__ import annotations

import re
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

_PATH_RE: Final = re.compile(
    rf"(?P<metric>{'|'.join(m.value for m in UsageMetric)})"
    r"_(?P<count>[1-9][0-9]{0,6})(?P<unit>[mhd])"
)
USAGE_PATH_GRAMMAR: Final = (
    f"<metric>_<N><m|h|d> between 1m and 30d; metrics: "
    f"{', '.join(m.value for m in UsageMetric)}"
)


class InvalidUsagePaths(ValueError):
    """The ``paths`` query parameter is empty, too long, or has a bad path. -> 422."""


@dataclass(frozen=True, slots=True, order=True)
class UsageWindowSpec:
    metric: UsageMetric
    window_seconds: int


def parse_usage_path(path: str) -> UsageWindowSpec:
    """Every unit is a whole number of minutes, so whole minutes hold by construction."""
    match = _PATH_RE.fullmatch(path)
    if match is None:
        raise _invalid_path(path)
    seconds = int(match["count"]) * _SECONDS_PER_UNIT[match["unit"]]
    if not MIN_WINDOW_SECONDS <= seconds <= MAX_WINDOW_SECONDS:
        raise _invalid_path(path)
    return UsageWindowSpec(UsageMetric(match["metric"]), seconds)


def parse_usage_paths(raw: str) -> dict[str, UsageWindowSpec]:
    """Keyed by the names as requested: ``invocations_60m`` and ``invocations_1h``
    are two keys with equal specs, because the caller drops keys it didn't ask for."""
    items = raw.split(PATH_SEPARATOR)
    if len(items) > MAX_PATHS_PER_REQUEST:
        raise InvalidUsagePaths(
            f"{len(items)} paths requested; at most {MAX_PATHS_PER_REQUEST} allowed"
        )
    return {item: parse_usage_path(item) for item in items}


def _invalid_path(path: str) -> InvalidUsagePaths:
    return InvalidUsagePaths(f"{path!r}: expected {USAGE_PATH_GRAMMAR}")
