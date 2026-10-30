"""The usage path grammar, and its parity with the SDK's path registries (G5)."""

from __future__ import annotations

import pytest

from hexgate.runtime import agent_usage
from hexgate.security import constraints
from hexgate_api.features.usage.paths import (
    MAX_PATHS_PER_REQUEST,
    USAGE_PATH_GRAMMAR,
    InvalidUsagePaths,
    UsageMetric,
    UsageWindowSpec,
    parse_usage_path,
    parse_usage_path_list,
    parse_usage_paths,
    parse_window_seconds,
)

USER_USAGE_ROOT = "user_usage"


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


@pytest.mark.parametrize(
    ("window", "seconds"), [("1m", 60), ("24h", 86_400), ("30d", 30 * 86_400)]
)
def test_a_bare_window_parses_to_seconds(window: str, seconds: int) -> None:
    assert parse_window_seconds(window) == seconds


@pytest.mark.parametrize("window", ["31d", "0h", "1w", "invocations_1h", ""])
def test_a_bad_bare_window_is_rejected(window: str) -> None:
    with pytest.raises(ValueError, match="expected"):
        parse_window_seconds(window)


@pytest.mark.parametrize("raw", ["", "invocations_1h,,denials_1h", "invocations_1h,"])
def test_an_empty_item_is_rejected(raw: str) -> None:
    with pytest.raises(InvalidUsagePaths, match="expected"):
        parse_usage_paths(raw)


def test_too_many_paths_are_rejected_before_any_is_parsed() -> None:
    raw = ",".join(["not_a_path"] * (MAX_PATHS_PER_REQUEST + 1))

    with pytest.raises(InvalidUsagePaths, match=f"at most {MAX_PATHS_PER_REQUEST}"):
        parse_usage_paths(raw)


def test_a_path_list_parses_like_the_query_parameter() -> None:
    items = ["invocations_60m", "invocations_1h", "total_tokens_24h"]

    assert parse_usage_path_list(items) == parse_usage_paths(",".join(items))


def test_an_empty_path_list_is_rejected() -> None:
    with pytest.raises(InvalidUsagePaths, match="no paths requested"):
        parse_usage_path_list([])


def test_too_many_listed_paths_are_rejected_before_any_is_parsed() -> None:
    with pytest.raises(InvalidUsagePaths, match=f"at most {MAX_PATHS_PER_REQUEST}"):
        parse_usage_path_list(["not_a_path"] * (MAX_PATHS_PER_REQUEST + 1))


def test_a_bad_listed_path_is_named() -> None:
    with pytest.raises(InvalidUsagePaths, match="'errors_1h'"):
        parse_usage_path_list(["invocations_1h", "errors_1h"])


def test_every_sdk_metric_is_a_platform_metric() -> None:
    sdk_metrics = {m.value for m in agent_usage.UsageMetric} | {"total_tokens"}

    assert sdk_metrics <= {m.value for m in UsageMetric}


def test_every_sdk_path_is_accepted() -> None:
    """G5: a path the SDK lints as valid but the endpoint rejects, or reads over a
    different window, would be wrong for every policy using it."""
    for name, sdk_path in agent_usage.AGENT_USAGE_PATHS.items():
        spec = parse_usage_path(name)
        assert spec.metric == sdk_path.metric, name
        assert spec.window_seconds == sdk_path.window_seconds, name


# Keyed on the root U2 cannot ship without, not on its registry's name: once the
# root exists, a missing or renamed USER_USAGE_PATHS fails here instead of skipping.
@pytest.mark.skipif(
    USER_USAGE_ROOT not in constraints.PATH_ROOTS, reason="needs U2 (user_usage root)"
)
def test_every_sdk_user_path_is_accepted() -> None:
    """G5 for user_usage.*: the same grammar serves both roots."""
    for name, sdk_path in agent_usage.USER_USAGE_PATHS.items():
        spec = parse_usage_path(name)
        assert spec.metric == sdk_path.metric, name
        assert spec.window_seconds == sdk_path.window_seconds, name
