"""The agent_usage path grammar, and its parity with the SDK's path registry (G5)."""

from __future__ import annotations

import pytest

from hexgate.runtime import agent_usage
from hexgate_api.features.usage.paths import (
    MAX_PATHS_PER_REQUEST,
    USAGE_PATH_GRAMMAR,
    InvalidUsagePaths,
    UsageMetric,
    UsageWindowSpec,
    parse_usage_path,
    parse_usage_paths,
)

# Only on 2b (#314). The skip is evaluated against the SDK each CI run installs
# (editable path dep), so whichever of #314 and this PR merges second runs it
# unskipped. Drop the skipif once both are on main.
_SDK_AGENT_USAGE_PATHS = getattr(agent_usage, "AGENT_USAGE_PATHS", None)


@pytest.mark.parametrize(
    ("path", "metric", "seconds"),
    [
        ("invocations_5m", UsageMetric.INVOCATIONS, 300),
        ("total_tokens_30d", UsageMetric.TOTAL_TOKENS, 30 * 86_400),
        ("tool_calls_1h", UsageMetric.TOOL_CALLS, 3_600),
        ("denials_1m", UsageMetric.DENIALS, 60),
        ("llm_calls_43200m", UsageMetric.LLM_CALLS, 30 * 86_400),
    ],
)
def test_a_valid_path_parses_to_its_metric_and_window(
    path: str, metric: UsageMetric, seconds: int
) -> None:
    assert parse_usage_path(path) == UsageWindowSpec(metric, seconds)


def test_60m_and_1h_are_the_same_window_under_two_requested_names() -> None:
    parsed = parse_usage_paths("invocations_60m,invocations_1h")

    assert list(parsed) == ["invocations_60m", "invocations_1h"]
    assert parsed["invocations_60m"] == parsed["invocations_1h"]


def test_a_repeated_path_collapses_into_one_key() -> None:
    assert list(parse_usage_paths("denials_1h,denials_1h")) == ["denials_1h"]


@pytest.mark.parametrize(
    "path",
    [
        "errors_1h",
        "agent_usage.invocations_1h",
        "invocations_1",
        "invocations_60s",
        "invocations_0m",
        "invocations_01h",
        "invocations_31d",
        "invocations_43201m",
        "INVOCATIONS_1H",
        " invocations_1h",
        "invocations_1h ",
        "invocations_1h\n",
        "",
    ],
)
def test_a_bad_path_is_rejected_with_the_grammar(path: str) -> None:
    with pytest.raises(InvalidUsagePaths, match="expected") as exc:
        parse_usage_path(path)

    assert USAGE_PATH_GRAMMAR in str(exc.value)


@pytest.mark.parametrize("raw", ["", "invocations_1h,,denials_1h", "invocations_1h,"])
def test_an_empty_item_is_rejected(raw: str) -> None:
    with pytest.raises(InvalidUsagePaths, match="empty path"):
        parse_usage_paths(raw)


def test_too_many_paths_are_rejected_before_any_is_parsed() -> None:
    raw = ",".join(["not_a_path"] * (MAX_PATHS_PER_REQUEST + 1))

    with pytest.raises(InvalidUsagePaths, match=f"at most {MAX_PATHS_PER_REQUEST}"):
        parse_usage_paths(raw)


def test_every_sdk_metric_is_a_platform_metric() -> None:
    sdk_metrics = {m.value for m in agent_usage.UsageMetric} | {"total_tokens"}

    assert sdk_metrics <= {m.value for m in UsageMetric}


@pytest.mark.skipif(_SDK_AGENT_USAGE_PATHS is None, reason="needs 2b (#314)")
def test_every_sdk_path_is_accepted() -> None:
    """G5: a path the SDK lints as valid but the endpoint rejects, or reads over a
    different window, would be wrong for every policy using it."""
    assert _SDK_AGENT_USAGE_PATHS is not None
    for name, sdk_path in _SDK_AGENT_USAGE_PATHS.items():
        spec = parse_usage_path(name)
        assert spec.metric == sdk_path.metric, name
        assert spec.window_seconds == sdk_path.window_seconds, name
